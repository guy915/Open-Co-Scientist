from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any, cast

from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.orchestration.repository.tasks import NewTask
from co_scientist.platform import db
from co_scientist.platform.db.models import ScientificTask

if TYPE_CHECKING:
    from co_scientist.domains.research_state.state import WorkflowState


def _cascade_cancel_downstream(
    task_id: str,
    candidates: list[Any],
    conn: sqlite3.Connection,
) -> None:
    """Cancelled predecessors poison their entire lookahead chain; queued
    dependents would otherwise keep the cohort alive forever.
    """
    from co_scientist.orchestration.engine_tasks.support import NODE_TASK_PREFIX

    _cancel_downstream(candidates, {task_id}, None, NODE_TASK_PREFIX, conn)


def _apply_single_queue_action(
    task_id: str,
    action: dict[str, Any],
    candidates: list[Any],
    conn: sqlite3.Connection,
) -> None:
    reason = str(action.get("reason") or "Supervisor queue update")
    kind = action.get("action")
    if kind == "cancel":
        if lifecycle.cancel_task(task_id, reason=reason, conn=conn):
            _cascade_cancel_downstream(task_id, candidates, conn)
        return
    if kind == "retry":
        lifecycle.retry_task(task_id, reason=reason, conn=conn)
        return
    priority = action.get("priority")
    if kind == "reprioritize" and priority is not None:
        lifecycle.reprioritize_task(task_id, int(priority), reason=reason, conn=conn)


def _apply_supervisor_queue_actions(
    run_id: str,
    actions: list[dict[str, Any]],
    conn: sqlite3.Connection,
) -> None:
    candidates = tasks.list_tasks(run_id, conn=conn)
    known_ids = {task.id for task in candidates}
    for action in actions[:8]:
        task_id = str(action.get("task_id") or "")
        if task_id in known_ids:
            _apply_single_queue_action(task_id, action, candidates, conn)


# Bound stacked follow-ups independently of the engine's current companion
# count.
_MAX_STACKED_TASKS = 4


def _apply_supervisor_enqueue_actions(
    predecessor: Any,
    actions: list[dict[str, Any]],
    priority: int,
    conn: sqlite3.Connection,
) -> None:
    """Same-run recognized tasks are chained serially; shared edge keys
    deduplicate planned companions and reactive successors.
    """
    from co_scientist.orchestration.engine_tasks.support import NODE_TASK_PREFIX
    from co_scientist.orchestration.workflow_topology import TASK_ROUTES
    from co_scientist.science.scheduling import stacked_task_values

    for value in stacked_task_values(actions)[:_MAX_STACKED_TASKS]:
        node = TASK_ROUTES.get(value)
        if node is None:
            continue
        predecessor = _enqueue_after(predecessor, f"{NODE_TASK_PREFIX}{node}", priority, conn)


def _durable_queue_snapshot(run_id: str, db_path: str | None) -> list[dict[str, Any]]:
    return [
        {
            "task_id": task.id,
            "task_type": task.task_type,
            "status": task.status,
            "priority": task.priority,
            "attempt": task.attempt,
            "max_attempts": task.max_attempts,
            "dependencies": list(task.dependencies),
            "error": task.error,
        }
        for task in tasks.list_tasks(run_id, db_path=db_path)[-100:]
        if task.status in {"queued", "leased", "paused", "failed"}
    ]


def _enqueue_after(
    predecessor: ScientificTask,
    task_type: str,
    priority: int,
    conn: sqlite3.Connection,
) -> ScientificTask:
    """Idempotency derives from task type and predecessor identity so
    planned and reactive enqueue resolve to the same edge.
    """
    successor = tasks.enqueue_task(
        NewTask(
            run_id=predecessor.run_id,
            task_type=task_type,
            inputs={},
            idempotency_key=f"{task_type}:after:{predecessor.id}",
            priority=lifecycle.clamp_task_priority(priority),
            dependencies=(predecessor.id,),
            provenance={"scheduled_by": predecessor.task_type},
        ),
        conn=conn,
    )
    # Run-level interruption cancelled forecast rows without a reason. Only
    # revive the edge selected now, never an explicitly cancelled branch.
    if successor.status == "cancelled" and successor.error is None:
        lifecycle.revive_task_for_retry(successor.run_id, successor.idempotency_key, conn=conn)
        refreshed = tasks.get_task(successor.id, conn=conn)
        assert refreshed is not None
        return refreshed
    return successor


def _is_stale_dependent(
    candidate: ScientificTask,
    poisoned_ids: set[str],
    protect_type: str | None,
    node_prefix: str,
) -> bool:
    """Only the real direct successor is protected; nothing behind a
    cancelled predecessor is a valid reuse.
    """
    if candidate.status != "queued":
        return False
    if not candidate.task_type.startswith(node_prefix):
        return False
    if not candidate.dependencies or candidate.dependencies[0] not in poisoned_ids:
        return False
    return protect_type is None or candidate.task_type != protect_type


def _cancel_dependents(
    candidates: list[ScientificTask],
    poisoned_ids: set[str],
    protect_type: str | None,
    node_prefix: str,
    conn: sqlite3.Connection,
) -> set[str]:
    cancelled: set[str] = set()
    for candidate in candidates:
        if not _is_stale_dependent(candidate, poisoned_ids, protect_type, node_prefix):
            continue
        lifecycle.cancel_task(
            candidate.id,
            reason="portfolio plan superseded by the real successor",
            conn=conn,
        )
        cancelled.add(candidate.id)
    return cancelled


def _cancel_downstream(
    candidates: list[ScientificTask],
    start_ids: set[str],
    protect_type: str | None,
    node_prefix: str,
    conn: sqlite3.Connection,
) -> None:
    poisoned = start_ids
    protect = protect_type
    while poisoned:
        poisoned = _cancel_dependents(candidates, poisoned, protect, node_prefix, conn)
        protect = None


def _cancel_stale_planned_chain(
    task: ScientificTask,
    keep_type: str,
    conn: sqlite3.Connection,
) -> None:
    """A changed route poisons every dependent lookahead row; unclaimable
    queued tails otherwise prevent settlement.
    """
    from co_scientist.orchestration.engine_tasks.support import NODE_TASK_PREFIX

    candidates = tasks.list_tasks(task.run_id, conn=conn)
    _cancel_downstream(candidates, {task.id}, keep_type, NODE_TASK_PREFIX, conn)


def cancel_downstream_portfolio_chain(task: ScientificTask, db_path: str | None) -> None:
    """Cancel downstream only on terminal failure and before failure
    settlement; ordinary retries must retain reusable successors.
    """
    from co_scientist.orchestration.engine_tasks.support import NODE_TASK_PREFIX

    with db.transaction(db_path) as conn:
        candidates = tasks.list_tasks(task.run_id, conn=conn)
        _cancel_downstream(candidates, {task.id}, None, NODE_TASK_PREFIX, conn)


def _enqueue_lookahead_tail(
    head: ScientificTask,
    successor: str,
    state: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    """Each deterministic hop depends on the row just created, never a
    sibling or unresolved aggregate.
    """
    from co_scientist.orchestration.engine_tasks.support import NODE_TASK_PREFIX
    from co_scientist.orchestration.task_runtime import plan_portfolio

    predecessor = head
    for hop in plan_portfolio(successor, cast("WorkflowState", state))[1:]:
        predecessor = _enqueue_after(predecessor, f"{NODE_TASK_PREFIX}{hop}", 90, conn)


def _enqueue_node_portfolio(
    task: ScientificTask,
    state: dict[str, Any],
    successor: str | None,
    successor_type: str,
    conn: sqlite3.Connection,
) -> ScientificTask:
    from co_scientist.orchestration.engine_tasks.support import NODE_TASK_PREFIX

    is_orchestrator = task.task_type == f"{NODE_TASK_PREFIX}orchestrator"
    if is_orchestrator:
        _apply_supervisor_queue_actions(
            task.run_id, state.get("supervisor_queue_actions") or [], conn
        )
    priority = int(state.get("next_task_priority", 90)) if is_orchestrator else 90
    _cancel_stale_planned_chain(task, successor_type, conn)
    head = _enqueue_after(task, successor_type, priority, conn)
    if is_orchestrator:
        # Enqueue companions after stale-plan cancellation, which would
        # otherwise sweep newly created rows.
        _apply_supervisor_enqueue_actions(
            task,
            state.get("supervisor_queue_actions") or [],
            priority,
            conn,
        )
    if successor is not None:
        _enqueue_lookahead_tail(head, successor, state, conn)
    return head
