from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    expires_at: float
    value: T


class AsyncTTLCache(Generic[T]):
    def __init__(self, ttl: float, max_entries: int, clock=time.monotonic):
        self.ttl = max(0.0, ttl)
        self.max_entries = max(1, max_entries)
        self.clock = clock
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> T | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self.clock():
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry.value

    async def set(self, key: str, value: T, ttl: float | None = None) -> None:
        async with self._lock:
            self._entries[key] = _Entry(self.clock() + (self.ttl if ttl is None else max(0.0, ttl)), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
