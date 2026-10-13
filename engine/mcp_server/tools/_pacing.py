import asyncio
import threading
import time


class RequestPacer:
    """Reserves process-wide request start slots; a thread lock, because callers
    run on several event loops."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        with self._lock:
            start = max(now, self._next_at)
            self._next_at = start + self._interval
        if start > now:
            await asyncio.sleep(start - now)
