import os
import threading
import weakref
from collections import Counter
from collections.abc import AsyncGenerator

from fastapi import HTTPException
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

_lock = threading.Lock()
_owners: Counter[str] = Counter()
_peers: Counter[str] = Counter()
_active = 0
_MAX_CONNECTIONS = int(os.getenv("SSE_MAX_CONNECTIONS", "256"))
if not 1 <= _MAX_CONNECTIONS <= 1024:
    raise ValueError("SSE_MAX_CONNECTIONS must be between 1 and 1024")


def _acquire(owner: str, peer: str) -> bool:
    global _active
    with _lock:
        if _active >= _MAX_CONNECTIONS or _owners[owner] >= 4 or _peers[peer] >= 8:
            return False
        _active += 1
        _owners[owner] += 1
        _peers[peer] += 1
        return True


def _release(owner: str, peer: str) -> None:
    global _active
    with _lock:
        _active -= 1
        for counters, key in ((_owners, owner), (_peers, peer)):
            counters[key] -= 1
            if not counters[key]:
                del counters[key]


class AdmittedEventStream(StreamingResponse):
    def __init__(self, content: AsyncGenerator[str, None], owner: str, peer: str):
        if not _acquire(owner, peer):
            raise HTTPException(
                status_code=429,
                detail="Live updates are busy. Close extra run tabs or try again in 30 seconds.",
                headers={"Retry-After": "30"},
            )
        try:
            super().__init__(
                content,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
            # Covers an off-loop response discarded before ASGI starts it.
            self._close_admission = weakref.finalize(self, _release, owner, peer)
        except BaseException:
            _release(owner, peer)
            raise

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._close_admission()
