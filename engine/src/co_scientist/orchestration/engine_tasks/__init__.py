from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from co_scientist.core.run_modes import resolved_run_config
from co_scientist.orchestration.engine_tasks import runtime as engine_tasks_runtime
from co_scientist.orchestration.engine_tasks.fanout import (
    execute_generation_strategy,
    execute_mature_reflection_item,
    execute_review_item,
    execute_verification_item,
)
from co_scientist.orchestration.engine_tasks.fanout_aggregates import (
    execute_generation_aggregate,
    execute_mature_reflection_aggregate,
    execute_review_aggregate,
    execute_verification_aggregate,
)
from co_scientist.orchestration.engine_tasks.finalize import execute_finalize as execute_finalize
from co_scientist.orchestration.engine_tasks.inputs import enqueue_bootstrap as enqueue_bootstrap
from co_scientist.orchestration.engine_tasks.inputs import (
    enqueue_scientist_continuation as enqueue_scientist_continuation,
)
from co_scientist.orchestration.engine_tasks.inputs import execute_bootstrap as execute_bootstrap
from co_scientist.orchestration.engine_tasks.node import execute_node_task as execute_node_task
from co_scientist.orchestration.engine_tasks.ranking import (
    execute_ranking_finalize,
    execute_ranking_match,
)
from co_scientist.orchestration.engine_tasks.support import BOOTSTRAP_TASK as BOOTSTRAP_TASK
from co_scientist.orchestration.engine_tasks.support import ENGINE_TASK_PREFIX as ENGINE_TASK_PREFIX
from co_scientist.orchestration.engine_tasks.support import (
    FINALIZE_TASK,
    GENERATION_AGGREGATE_TASK,
    GENERATION_STRATEGY_TASK,
    MATURE_REFLECTION_AGGREGATE_TASK,
    RANKING_FINALIZE_TASK,
    RANKING_MATCH_TASK,
    REVIEW_AGGREGATE_TASK,
    REVIEW_ITEM_TASK,
    VERIFICATION_AGGREGATE_TASK,
    VERIFICATION_ITEM_TASK,
)
from co_scientist.orchestration.engine_tasks.support import (
    MATURE_REFLECTION_ITEM_TASK as MATURE_REFLECTION_ITEM_TASK,
)
from co_scientist.orchestration.engine_tasks.support import NODE_TASK_PREFIX as NODE_TASK_PREFIX
from co_scientist.orchestration.engine_tasks.support import SafetyHoldError as SafetyHoldError
from co_scientist.orchestration.engine_tasks.support import (
    SupersededTaskError as SupersededTaskError,
)
from co_scientist.platform.db import runs
from co_scientist.platform.db.llm_routes import route_for_run
from co_scientist.platform.db.models import ScientificTask
from co_scientist.platform.llm.admission.service import current_db_path
from co_scientist.platform.llm.execution_policy import (
    zero_cost_admission_for_config,
)
from co_scientist.platform.llm.routing import scoped_operator_routing

_ENGINE_TASK_DISPATCH: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    BOOTSTRAP_TASK: execute_bootstrap,
    REVIEW_ITEM_TASK: execute_review_item,
    REVIEW_AGGREGATE_TASK: execute_review_aggregate,
    VERIFICATION_ITEM_TASK: execute_verification_item,
    VERIFICATION_AGGREGATE_TASK: execute_verification_aggregate,
    RANKING_MATCH_TASK: execute_ranking_match,
    RANKING_FINALIZE_TASK: execute_ranking_finalize,
    GENERATION_STRATEGY_TASK: execute_generation_strategy,
    GENERATION_AGGREGATE_TASK: execute_generation_aggregate,
    MATURE_REFLECTION_ITEM_TASK: execute_mature_reflection_item,
    MATURE_REFLECTION_AGGREGATE_TASK: execute_mature_reflection_aggregate,
    FINALIZE_TASK: execute_finalize,
}


async def _dispatch_engine_task(task: ScientificTask, *, db_path: str | None) -> dict[str, Any]:
    # Retired queued rows settle without issuing provider calls or losing the
    # checkpoint that ordinary recovery resumes.
    if task.task_type == "engine.outcome.refinement":
        return {"retired": True}
    handler = _ENGINE_TASK_DISPATCH.get(task.task_type)
    if handler is not None:
        return await handler(task, db_path=db_path)
    if task.task_type.startswith(NODE_TASK_PREFIX):
        return await execute_node_task(task, db_path=db_path)
    raise ValueError(f"unsupported engine task: {task.task_type}")


async def execute_engine_task(
    task: ScientificTask, *, db_path: str | None = None
) -> dict[str, Any]:
    """Credential and call-budget scopes cover the whole task, including
    nested retries, without mutating shared process state.
    """
    from co_scientist.core.byok_scope import scoped_byok
    from co_scientist.domains.access.credentials import get_run_credential
    from co_scientist.platform.db.admission import run_host
    from co_scientist.platform.llm import (
        scoped_api_key,
        scoped_llm_call_budget,
        scoped_zero_cost_admission,
    )
    from co_scientist.platform.llm.provider_usage import scoped_client

    run = runs.get_run(task.run_id, db_path=db_path)
    if run is None:
        raise LookupError(f"run not found for task dispatch: {task.run_id}")
    credential = get_run_credential(task.run_id, db_path=db_path)
    ceiling = resolved_run_config(run.config).get("max_llm_calls")
    ceiling = int(ceiling) if isinstance(ceiling, int) else None
    with (
        engine_tasks_runtime.bound(engine_tasks_runtime.active()),
        scoped_byok(credential),
        scoped_client(run.client_id, host=run_host(run.id, db_path=db_path), db_path=db_path),
        scoped_api_key(
            credential.api_key if credential else None,
            by_model=credential.keys_by_model() if credential else None,
        ),
        scoped_llm_call_budget(task.run_id, ceiling),
        scoped_zero_cost_admission(zero_cost_admission_for_config(run.config)),
        scoped_operator_routing(
            run.id,
            enabled=credential is None and route_for_run(run.id, current_db_path()) is not None,
        ),
    ):
        return await _dispatch_engine_task(task, db_path=db_path)
