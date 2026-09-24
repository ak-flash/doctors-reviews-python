from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class FixedWindowRateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit = max(1, limit)
        self.window = max(0.001, window)
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        async with self._lock:
            start, count = self._buckets.get(key, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            if count >= self.limit:
                return False, max(0.0, self.window - (now - start))
            self._buckets[key] = (start, count + 1)
            return True, max(0.0, self.window - (now - start))
