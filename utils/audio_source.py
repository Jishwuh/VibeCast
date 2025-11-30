import os
import re
import logging
from typing import Optional

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
    # Use alternative player clients to bypass some age/region restrictions
    "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
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

    def _yt_opts(self) -> dict:
        opts = dict(BASE_YDL_OPTS)
        if self.youtube_cookies:
            if os.path.exists(self.youtube_cookies):
                opts["cookiefile"] = self.youtube_cookies
            else:
                self.logger.warning("YouTube cookies file not found at %s", self.youtube_cookies)
        return opts

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
        with yt_dlp.YoutubeDL(self._yt_opts()) as ydl:
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
            source=source_override or self._guess_source(url),
            stream_url=stream_url,
            headers=headers,
        )
        return track

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

    @staticmethod
    def _guess_source(url: str) -> str:
        if "spotify.com" in url:
            return "Spotify"
        if "soundcloud.com" in url:
            return "SoundCloud"
        return "YouTube"
