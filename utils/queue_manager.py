import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class Track:
    title: str
    url: str
    requester: str
    duration: Optional[int]
    source: str
    stream_url: str
    headers: Optional[dict] = None


class QueueManager:
    def __init__(self, max_length: int = 50):
        self._queues: Dict[int, List[Track]] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self.max_length = max_length

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    async def add_track(self, guild_id: int, track: Track) -> bool:
        async with self._get_lock(guild_id):
            queue = self._queues.setdefault(guild_id, [])
            if len(queue) >= self.max_length:
                return False
            queue.append(track)
            return True

    async def pop_next(self, guild_id: int) -> Optional[Track]:
        async with self._get_lock(guild_id):
            queue = self._queues.get(guild_id, [])
            if queue:
                return queue.pop(0)
            return None

    async def peek(self, guild_id: int) -> Optional[Track]:
        async with self._get_lock(guild_id):
            queue = self._queues.get(guild_id, [])
            return queue[0] if queue else None

    async def clear(self, guild_id: int):
        async with self._get_lock(guild_id):
            self._queues[guild_id] = []

    async def list_queue(self, guild_id: int) -> List[Track]:
        async with self._get_lock(guild_id):
            return list(self._queues.get(guild_id, []))

    async def size(self, guild_id: int) -> int:
        async with self._get_lock(guild_id):
            return len(self._queues.get(guild_id, []))

    async def is_empty(self, guild_id: int) -> bool:
        return await self.size(guild_id) == 0
