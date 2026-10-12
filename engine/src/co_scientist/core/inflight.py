"""Provider calls whose outcome this process does not know yet, per durable task.

Graceful shutdown releases a task's lease only when none of its provider calls
is unanswered: a call that was dispatched and never answered (timed out,
cancelled, still streaming) may already have been billed, so its task keeps
the lease and the unknown-outcome rule decides it after expiry.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current_task: ContextVar[str | None] = ContextVar("cosci_inflight_task", default=None)
_lock = threading.Lock()
_unanswered: dict[str, int] = {}
_shutting_down = threading.Event()


@contextmanager
def task_scope(task_id: str) -> Iterator[None]:
    """Child asyncio tasks created inside the scope inherit the task id."""
    token = _current_task.set(task_id)
    try:
        yield
    finally:
        _current_task.reset(token)


class CallMark:
    def __init__(self, task_id: str | None) -> None:
        self._task_id = task_id

    def answered(self) -> None:
        """The provider responded (success or error), so its outcome is known."""
        task_id, self._task_id = self._task_id, None
        if task_id is None:
            return
        with _lock:
            remaining = _unanswered.get(task_id, 0) - 1
            if remaining > 0:
                _unanswered[task_id] = remaining
            else:
                _unanswered.pop(task_id, None)


async def begin_provider_call() -> CallMark:
    """Mark a dispatch, or park it until cancelled once shutdown has begun:
    a call started after the release snapshot would run on a released task.
    """
    task_id = _current_task.get()
    if task_id is None:
        return CallMark(None)
    with _lock:
        parked = _shutting_down.is_set()
        if not parked:
            _unanswered[task_id] = _unanswered.get(task_id, 0) + 1
    if parked:
        await asyncio.Event().wait()
    return CallMark(task_id)


def begin_shutdown() -> frozenset[str]:
    """Stop new dispatches and return the tasks whose calls are unanswered."""
    with _lock:
        _shutting_down.set()
        return frozenset(_unanswered)


def shutting_down() -> bool:
    return _shutting_down.is_set()


def resume_dispatch() -> None:
    """A lifespan restart in the same process serves again."""
    _shutting_down.clear()
