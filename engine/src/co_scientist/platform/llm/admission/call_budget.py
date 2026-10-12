"""Count physical requests inside tasks to catch node-local overruns.
Threading locks cross worker event loops without asyncio affinity.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from co_scientist.core._context import _bind_contextvar
from co_scientist.core.exceptions import LLMCallBudgetExceededError
from co_scientist.platform.db.call_admission import (
    ensure_run_counter,
    read_run_count,
    reserve_run_call,
)
from co_scientist.platform.llm.admission.service import current_db_path


@dataclass
class CompletionBudget:
    """Operation budgets are shared by concurrent child tasks, independently
    of run counters.
    """

    ceiling: int
    count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reserve(self) -> None:
        with self._lock:
            if self.count >= self.ceiling:
                raise LLMCallBudgetExceededError(self.count + 1, self.ceiling)
            self.count += 1


_current: ContextVar[CompletionBudget | None] = ContextVar(
    "completion_operation_budget", default=None
)


def current_completion_budget() -> CompletionBudget | None:
    return _current.get()


@contextmanager
def scoped_completion_budget(ceiling: int) -> Iterator[CompletionBudget]:
    if ceiling < 1:
        raise ValueError("completion budget must allow at least one call")
    budget = CompletionBudget(ceiling)
    with _bind_contextvar(_current, budget):
        yield budget


logger = logging.getLogger(__name__)

# The cache is a task/scheduler view; durable rows enforce bounded dispatches.
_MAX_TRACKED_RUNS = 500


@dataclass(frozen=True)
class _RunScope:
    run_id: str
    db_path: str
    durable: bool


_current_run: ContextVar[_RunScope | None] = ContextVar("llm_call_budget_run", default=None)


@dataclass
class _RunCounter:
    count: int
    ceiling: int | None
    durable: bool = False


_lock = threading.Lock()
# Re-entered runs move last so eviction still identifies the oldest entry.
_runs: OrderedDict[tuple[str, str], _RunCounter] = OrderedDict()


@contextmanager
def scoped_llm_call_budget(run_id: str | None, ceiling: int | None) -> Iterator[None]:
    """Tasks re-enter one persisted run counter without resetting it; the
    first ceiling remains binding.
    """
    scope = _ensure_tracked(run_id, ceiling) if run_id is not None else None
    with _bind_contextvar(_current_run, scope):
        yield


def _ensure_tracked(run_id: str, ceiling: int | None) -> _RunScope:
    db_path = current_db_path()
    scope = _RunScope(run_id, db_path, ensure_run_counter(run_id, ceiling, db_path=db_path))
    key = (db_path, run_id)
    with _lock:
        if key in _runs:
            _runs.move_to_end(key)
            return scope
        _runs[key] = _RunCounter(count=0, ceiling=ceiling, durable=scope.durable)
        while len(_runs) > _MAX_TRACKED_RUNS:
            _runs.popitem(last=False)
    return scope


def record_provider_request() -> None:
    """Unscoped calls must not inherit the last run on this thread. The
    refused next request shares the scheduler ceiling boundary.
    """
    operation = current_completion_budget()
    if operation is not None:
        operation.reserve()
        return
    scope = _current_run.get()
    if scope is None:
        return
    if scope.durable:
        reserve_run_call(scope.run_id, db_path=scope.db_path)
        return
    key = (scope.db_path, scope.run_id)
    with _lock:
        entry = _runs.setdefault(key, _RunCounter(count=0, ceiling=None))
        entry.count += 1
        count, ceiling = entry.count, entry.ceiling
        _runs.move_to_end(key)
    if ceiling is not None and count > ceiling:
        raise LLMCallBudgetExceededError(count, ceiling)


def current_run_call_count(run_id: str) -> int:
    db_path = current_db_path()
    with _lock:
        entry = _runs.get((db_path, run_id))
        scope = _current_run.get()
        durable = (entry is not None and entry.durable) or (
            scope is not None and scope.run_id == run_id and scope.durable
        )
        count = entry.count if entry is not None else 0
    return (read_run_count(run_id, db_path=db_path) or 0) if durable else count


def release_run_call_budget(run_id: str) -> None:
    """Release process memory without refunding durable reservations."""
    with _lock:
        _runs.pop((current_db_path(), run_id), None)
