"""Provider calls with potentially billable unknown outcomes, per durable task.

Graceful shutdown releases a task's lease only when none of its provider calls
is unanswered: a call that was dispatched and never answered (timed out,
cancelled, still streaming) may already have been billed, so its task keeps
the lease and the unknown-outcome rule decides it after expiry. Proven-free
failures carry no charge uncertainty and can release their accounting marks.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current_task: ContextVar[str | None] = ContextVar("cosci_inflight_task", default=None)
_lock = threading.Lock()
_unanswered: dict[str | None, int] = {}
_shutting_down = threading.Event()
_quiescing = threading.Event()
_operations = 0


class _Operation:
    def __init__(self) -> None:
        self.active = True


_operation: ContextVar[_Operation | None] = ContextVar("cosci_operation", default=None)


@contextmanager
def operation_scope() -> Iterator[bool]:
    global _operations
    with _lock:
        admitted = not (_quiescing.is_set() or _shutting_down.is_set())
        if admitted:
            _operations += 1
    if not admitted:
        yield False
        return
    operation = _Operation()
    token = _operation.set(operation)
    try:
        yield True
    finally:
        with _lock:
            operation.active = False
            _operations -= 1
        _operation.reset(token)


def quiesce_status(*, begin: bool = False) -> dict[str, int | bool]:
    with _lock:
        if begin:
            _quiescing.set()
        calls = sum(_unanswered.values())
        return {
            "quiescing": _quiescing.is_set(),
            "active_operations": _operations,
            "unanswered_calls": calls,
            "ready": _quiescing.is_set() and _operations == 0 and calls == 0,
        }


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
        self._released = False

    def answered(self) -> None:
        """The provider responded (success or error), so its outcome is known."""
        self._release()

    def release_proven_zero_cost(self) -> None:
        """A failed call proven free no longer blocks task recovery."""
        self._release()

    def _release(self) -> None:
        with _lock:
            if self._released:
                return
            self._released = True
            task_id = self._task_id
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
    with _lock:
        operation = _operation.get()
        # Accepted work must finish all its calls and commit before the
        # deployment barrier closes; copied contexts cannot outlive that work.
        parked = _shutting_down.is_set() or (
            _quiescing.is_set() and (operation is None or not operation.active)
        )
        if not parked:
            _unanswered[task_id] = _unanswered.get(task_id, 0) + 1
    if parked:
        await asyncio.Event().wait()
    return CallMark(task_id)


def begin_shutdown() -> frozenset[str]:
    """Stop new dispatches and return the tasks whose calls are unanswered."""
    with _lock:
        _shutting_down.set()
        return frozenset(task for task in _unanswered if task is not None)


def shutting_down() -> bool:
    return _shutting_down.is_set() or _quiescing.is_set()


def shutdown_started() -> bool:
    return _shutting_down.is_set()


def resume_dispatch() -> None:
    """A lifespan restart in the same process serves again."""
    _shutting_down.clear()
    _quiescing.clear()
