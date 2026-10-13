from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from co_scientist.domains.safety.gate import ScreenSubject
    from co_scientist.domains.safety.types import SafetyDecision
    from co_scientist.platform.db.models import RunRow, ScientificTask

__all__ = [
    "EngineTaskRuntime",
    "ProductionEngineTaskRuntime",
    "active",
    "bound",
]


class EngineTaskRuntime(Protocol):
    """Task collaborators retain no generators or asyncio primitives across
    cohort event loops.
    """

    def generator_and_opts(
        self, task: ScientificTask, db_path: str | None
    ) -> tuple[Any, dict[str, Any]]: ...

    def generator_for_restore(self, task: ScientificTask, db_path: str | None) -> Any: ...

    async def screen(
        self,
        run_id: str,
        subject: ScreenSubject,
        *,
        provider: str,
        db_path: str | None = None,
    ) -> SafetyDecision: ...

    async def drain_final_state(
        self,
        run: RunRow,
        state: dict[str, Any],
        db_path: str | None,
    ) -> tuple[Any, float, dict[str, Any]]: ...


class ProductionEngineTaskRuntime:
    """Local imports keep this leaf below collaborators in the import graph,
    avoiding adapter/report cycles.
    """

    def generator_and_opts(
        self, task: ScientificTask, db_path: str | None
    ) -> tuple[Any, dict[str, Any]]:
        from co_scientist.orchestration.engine_tasks.support import _generator_and_opts

        return _generator_and_opts(task, db_path)

    def generator_for_restore(self, task: ScientificTask, db_path: str | None) -> Any:
        from co_scientist.orchestration.engine_tasks.support import _generator_for_restore

        return _generator_for_restore(task, db_path)

    async def screen(
        self,
        run_id: str,
        subject: ScreenSubject,
        *,
        provider: str,
        db_path: str | None = None,
    ) -> SafetyDecision:
        from co_scientist.domains.safety.gate import screen_with_escalation

        return await screen_with_escalation(run_id, subject, provider=provider, db_path=db_path)

    async def drain_final_state(
        self,
        run: RunRow,
        state: dict[str, Any],
        db_path: str | None,
    ) -> tuple[Any, float, dict[str, Any]]:
        from co_scientist.orchestration.engine_tasks.finalize import _drain_and_persist_final_state

        return await _drain_and_persist_final_state(run, state, db_path)


_installed: EngineTaskRuntime = ProductionEngineTaskRuntime()
_bound: ContextVar[EngineTaskRuntime | None] = ContextVar("engine_task_runtime", default=None)


def active() -> EngineTaskRuntime:
    """Call-time lookup supports directly invoked handlers while a
    dispatched task binds one consistent adapter.
    """
    return _bound.get() or _installed


@contextlib.contextmanager
def bound(runtime: EngineTaskRuntime) -> Iterator[None]:
    token = _bound.set(runtime)
    try:
        yield
    finally:
        _bound.reset(token)
