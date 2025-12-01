import os
import re
import logging
from typing import Optional, List, Dict

import yt_dlp

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:
    spotipy = None
    SpotifyClientCredentials = None

from .queue_manager import Track


BASE_YDL_OPTS = {
    "format": "bestaudio[ext=webm]/bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}


class AudioSource:
    def __init__(self, config: dict):
        self.config = config
        self.spotify_enabled = config.get("enable_spotify", False)
        self.youtube_api_key = os.getenv("YOUTUBE_API_KEY") or config.get("youtube_api_key") or ""
        self.spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID") or config.get("spotify_client_id") or ""
        self.spotify_client_secret = (
            os.getenv("SPOTIFY_CLIENT_SECRET") or config.get("spotify_client_secret") or ""
        )
        self.spotify_client = self._build_spotify_client()
        self.youtube_cookies = os.getenv("YOUTUBE_COOKIES") or config.get("youtube_cookies_file") or ""
        self.youtube_po_token = os.getenv("YOUTUBE_PO_TOKEN") or config.get("youtube_po_token") or ""
        self.logger = logging.getLogger("AudioSource")

    def _build_spotify_client(self):
        if not self.spotify_enabled:
            return None
        if not (self.spotify_client_id and self.spotify_client_secret):
            return None
        if spotipy is None:
            return None
        creds = SpotifyClientCredentials(
            client_id=self.spotify_client_id,
            client_secret=self.spotify_client_secret,
        )
        return spotipy.Spotify(auth_manager=creds, requests_timeout=10, retries=3)

    @staticmethod
    def _is_url(query: str) -> bool:
        return query.startswith("http://") or query.startswith("https://")

    @staticmethod
    def _is_spotify_url(url: str) -> bool:
        return "open.spotify.com" in url and "/track/" in url

    @staticmethod
    def _is_soundcloud_url(url: str) -> bool:
        return "soundcloud.com" in url

    def _yt_opts(self, overrides: Optional[dict] = None) -> dict:
        opts = dict(BASE_YDL_OPTS)
        if self.youtube_cookies:
            if os.path.exists(self.youtube_cookies):
                opts["cookiefile"] = self.youtube_cookies
            else:
                self.logger.warning("YouTube cookies file not found at %s", self.youtube_cookies)
        extractor_args = {"youtube": {"player_client": ["web"]}}
        if self.youtube_po_token:
            extractor_args["youtube"]["po_token"] = [self.youtube_po_token]
        opts["extractor_args"] = extractor_args
        if overrides:
            for k, v in overrides.items():
                if isinstance(v, dict) and isinstance(opts.get(k), dict):
                    merged = dict(opts[k])
                    merged.update(v)
                    opts[k] = merged
                else:
                    opts[k] = v
        return opts

    def _try_extract(self, query: str, opts: dict, requester: str) -> Track:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if info is None:
            raise ValueError("No results found.")
        if "entries" in info:
            info = info["entries"][0]
        title = info.get("title") or "Unknown title"
        url = info.get("webpage_url") or info.get("url") or query
        duration = info.get("duration")
        stream_url = info.get("url")
        headers = info.get("http_headers") or {}
        if not stream_url:
            raise ValueError("Unable to fetch stream URL.")
        track = Track(
            title=title,
            url=url,
            requester=requester,
            duration=duration,
            source=self._guess_source(url),
            stream_url=stream_url,
            headers=headers,
        )
        return track

    def resolve(self, query: str, requester: str) -> Track:
        query = query.strip()
        if self._is_url(query):
            if self._is_spotify_url(query):
                track_from_spotify = self._from_spotify(query, requester)
                if track_from_spotify is None:
                    raise ValueError("Unable to resolve Spotify track. Check credentials or link.")
                return track_from_spotify
            source = "SoundCloud" if self._is_soundcloud_url(query) else "YouTube"
            return self._from_ytdlp(query, requester, source_override=source)
        return self._from_ytdlp(f"ytsearch1:{query}", requester, source_override="YouTube")

    def _from_ytdlp(self, query: str, requester: str, source_override: Optional[str] = None) -> Track:
        errors = []
        attempts = [
            self._yt_opts({"extractor_args": {"youtube": {"player_client": ["android"]}}}),
            self._yt_opts({"extractor_args": {"youtube": {"player_client": ["tvembedded"]}}}),
            self._yt_opts(),
            self._yt_opts({"format": "bestaudio/best"}),
        ]
        for opts in attempts:
            try:
                track = self._try_extract(query, opts, requester)
                if source_override:
                    track.source = source_override
                return track
            except Exception as exc:
                errors.append(str(exc))
                continue
        raise ValueError("Unable to fetch stream URL. Errors: " + " | ".join(errors))

    def _from_spotify(self, url: str, requester: str) -> Optional[Track]:
        if not self.spotify_client:
            return None
        try:
            match = re.search(r"/track/([A-Za-z0-9]+)", url)
            if not match:
                return None
            track_id = match.group(1)
            meta = self.spotify_client.track(track_id)
        except Exception:
            return None
        name = meta.get("name", "Unknown track")
        artists = ", ".join(artist["name"] for artist in meta.get("artists", []))
        duration_ms = meta.get("duration_ms")
        duration = int(duration_ms / 1000) if duration_ms else None
        query = f"{name} {artists}"
        track = self._from_ytdlp(f"ytsearch1:{query}", requester, source_override="Spotify")
        # Preserve Spotify link as the track URL for context
        track.url = url
        track.duration = duration
        return track

    def fetch_playlist_entries(self, url: str, limit: int = 50) -> List[Dict[str, str]]:
        entries: List[Dict[str, str]] = []
        if self._is_spotify_url(url) and self.spotify_client and "/playlist/" in url:
            try:
                playlist_id = re.search(r"/playlist/([A-Za-z0-9]+)", url)
                if playlist_id:
                    resp = self.spotify_client.playlist_tracks(playlist_id.group(1), limit=limit)
                    for item in resp.get("items", []):
                        track = item.get("track") or {}
                        name = track.get("name")
                        artists = ", ".join(a["name"] for a in track.get("artists", []))
                        if name:
                            entries.append({"title": name, "query": f"{name} {artists}".strip(), "url": track.get("external_urls", {}).get("spotify", url)})
            except Exception:
                pass
            return entries

        # YouTube playlist or generic; use yt_dlp flat extraction
        ydl_opts = {
            "quiet": True,
            "extract_flat": True,
            "skip_download": True,
            "playlistend": limit,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and "entries" in info:
                for item in info["entries"][:limit]:
                    title = item.get("title") or "Unknown"
                    link = item.get("url") or item.get("webpage_url") or url
                    entries.append({"title": title, "query": title, "url": link})
        except Exception:
            # fallback: treat as single item
            pass
        return entries

    @staticmethod
    def _guess_source(url: str) -> str:
        if "spotify.com" in url:
            return "Spotify"
        if "soundcloud.com" in url:
            return "SoundCloud"
        return "YouTube"
