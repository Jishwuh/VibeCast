import json
from pathlib import Path
from typing import Dict, List, Optional


class PlaylistStore:
    def __init__(self, path: Path = Path("data/playlists.json")):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict[str, List[dict]]] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fp:
                    self._data = json.load(fp)
            except Exception:
                self._data = {}
        else:
            self._data = {}

    def _save(self):
        with self.path.open("w", encoding="utf-8") as fp:
            json.dump(self._data, fp, indent=2)

    def list_playlists(self, guild_id: int) -> List[str]:
        guild_key = str(guild_id)
        return sorted(self._data.get(guild_key, {}).keys())

    def save_playlist(self, guild_id: int, name: str, tracks: List[dict]):
        guild_key = str(guild_id)
        self._data.setdefault(guild_key, {})[name] = tracks
        self._save()

    def append_track(self, guild_id: int, name: str, track: dict):
        guild_key = str(guild_id)
        playlist = self._data.setdefault(guild_key, {}).setdefault(name, [])
        playlist.append(track)
        self._save()

    def delete_playlist(self, guild_id: int, name: str) -> bool:
        guild_key = str(guild_id)
        if guild_key in self._data and name in self._data[guild_key]:
            del self._data[guild_key][name]
            self._save()
            return True
        return False

    def get_playlist(self, guild_id: int, name: str) -> Optional[List[dict]]:
        guild_key = str(guild_id)
        return self._data.get(guild_key, {}).get(name)
