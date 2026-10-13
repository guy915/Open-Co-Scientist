from __future__ import annotations

import threading
from collections.abc import Sequence

from co_scientist.core.admission_windows import WindowCapacity, check_capacity


def reserve_memory_window(
    hits_by_scope: dict[str, list[float]],
    scopes: Sequence[str],
    *,
    lock: threading.Lock,
    limit: int,
    now: float,
    detail: str,
    seconds: float = 60.0,
) -> None:
    if limit <= 0:
        return
    with lock:
        for stale in [
            key for key, hits in hits_by_scope.items() if not hits or now - hits[-1] >= seconds
        ]:
            del hits_by_scope[stale]
        active = {
            scope: [hit for hit in hits_by_scope.get(scope, []) if now - hit < seconds]
            for scope in dict.fromkeys(scopes)
        }
        check_capacity(
            (
                WindowCapacity(len(hits), limit, hits[max(0, len(hits) - limit)] + seconds)
                for hits in active.values()
                if len(hits) >= limit
            ),
            now=now,
            detail=detail,
        )
        # A refusal must not charge a sibling scope for an unaccepted request.
        for scope, hits in active.items():
            hits_by_scope[scope] = [*hits, now]
