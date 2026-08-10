from __future__ import annotations

import asyncio
import time


class FixedWindowRateLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit = limit_per_minute
        self._lock = asyncio.Lock()
        self._windows: dict[tuple[str, int], int] = {}

    async def allow(self, key: str) -> bool:
        window = int(time.monotonic() // 60)
        scoped = (key, window)
        async with self._lock:
            count = self._windows.get(scoped, 0)
            if count >= self.limit:
                return False
            self._windows[scoped] = count + 1
            if len(self._windows) > 10_000:
                self._windows = {
                    candidate: value
                    for candidate, value in self._windows.items()
                    if candidate[1] >= window - 1
                }
        return True
