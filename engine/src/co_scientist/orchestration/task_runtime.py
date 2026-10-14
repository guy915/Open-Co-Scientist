"""The app owns leases and transactions; this runtime owns scientific state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from co_scientist.domains.research_state.models import create_metrics_update, merge_metrics
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.orchestration.registry import NODE_REGISTRY
from co_scientist.orchestration.workflow_topology import (
    WORKFLOW_ROUTES,
    LiteratureGated,
    Route,
)
from co_scientist.platform.llm import scoped_telemetry

TaskNode = Callable[[WorkflowState], Awaitable[dict[str, Any]]]

# Registry keys are persisted in durable tasks and checkpoints.
TASK_NODES: dict[str, TaskNode] = {key: spec.node for key, spec in NODE_REGISTRY.items()}


Reducer = Callable[[Any, Any], Any]


def apply_task_update(state: WorkflowState, update: dict[str, Any]) -> WorkflowState:
    merged: dict[str, Any] = dict(state)
    for key, value in update.items():
        reducer = _CHANNEL_REDUCERS.get(key)
        if reducer is None:
            merged[key] = value
            continue
        merged[key] = reducer(state.get(key), value)
    return merged  # type: ignore[return-value]


def channel_reducers(state_type: type) -> dict[str, Reducer]:
    reducers: dict[str, Reducer] = {}
    hints = get_type_hints(state_type, include_extras=True)
    for name, hint in hints.items():
        if get_origin(hint) is not Annotated:
            continue
        base, *metadata = get_args(hint)
        reducers[name] = _from_empty(metadata[-1], base)
    return reducers


def _from_empty(reducer: Reducer, base: Any) -> Reducer:
    if get_origin(base) is not list:
        return reducer
    return lambda existing, value: reducer(existing or [], value)


_CHANNEL_REDUCERS = channel_reducers(WorkflowState)


def _resolve(route: Route, state: WorkflowState) -> str | None:
    if isinstance(route, LiteratureGated):
        return route.pick(bool(state.get("mcp_available")))
    return route(state) if callable(route) else route


def next_task_type(completed: str, state: WorkflowState) -> str | None:
    if completed not in WORKFLOW_ROUTES:
        raise ValueError(f"unsupported completed task node: {completed}")
    # Mid-flight safety stops must halt immediately, before the next loop point.
    if state.get("safety_blocked"):
        return None
    return _resolve(WORKFLOW_ROUTES[completed], state)


# Fan-out size and orchestrator successors exist only after execution;
# portfolios must stop there.
FANNING_NODES = frozenset(
    {
        "generate",
        "ranking",
        "review",
        "comprehensive_reflection",
        "deep_verification",
    }
)
_PORTFOLIO_STOP_NODES = FANNING_NODES | {"orchestrator"}

# Bound deterministic walks even if future routing accidentally cycles.
_MAX_PORTFOLIO_DEPTH = 4

# Resolve live-state routes ahead of execution only when their required state is
# already committed.
_RESOLVER_REQUIRES: dict[str, str] = {
    "supervisor": "mcp_available",
    "generate": "mcp_available",
    # Meta-review's successor is the orchestrator decision that scheduled
    # it; a plan built before that decision exists must not guess evolve.
    "meta_review": "next_task",
    # Same for the overview node: a plan built before the decision exists
    # must not guess that this firing is the terminal one.
    "research_overview": "next_task",
}


def _resolve_walkable_hop(current: str, state: WorkflowState) -> str | None:
    required = _RESOLVER_REQUIRES.get(current)
    if required is not None and required not in state:
        return None
    route = WORKFLOW_ROUTES.get(current)
    if route is None:
        return None
    return _resolve(route, state)


def plan_portfolio(start: str, state: WorkflowState) -> list[str]:
    chain = [start]
    current = start
    while current not in _PORTFOLIO_STOP_NODES and len(chain) < _MAX_PORTFOLIO_DEPTH:
        next_hop = _resolve_walkable_hop(current, state)
        if next_hop is None:
            break
        chain.append(next_hop)
        current = next_hop
    return chain


def _fold_telemetry_into_update(
    update: dict[str, Any], usage: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if not usage:
        return update
    telemetry_metrics = create_metrics_update(model_usage=usage)
    existing_metrics = update.get("metrics")
    merged_metrics = (
        merge_metrics(existing_metrics, telemetry_metrics)
        if existing_metrics is not None
        else telemetry_metrics
    )
    return {**update, "metrics": merged_metrics}


async def execute_task_node(
    task_type: str, state: WorkflowState
) -> tuple[WorkflowState, str | None]:
    try:
        handler = TASK_NODES[task_type]
    except KeyError as exc:
        raise ValueError(f"unsupported task node: {task_type}") from exc
    with scoped_telemetry(task_type) as telemetry:
        update = await handler(state)
    update = _fold_telemetry_into_update(update, telemetry.snapshot())
    committed = apply_task_update(state, update)
    return committed, next_task_type(task_type, committed)


__all__ = [
    "FANNING_NODES",
    "TASK_NODES",
    "apply_task_update",
    "channel_reducers",
    "execute_task_node",
    "next_task_type",
    "plan_portfolio",
]
