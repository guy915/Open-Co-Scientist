from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from co_scientist.orchestration.engine_tasks import runtime as engine_tasks_runtime
from co_scientist.orchestration.engine_tasks.fanout import (
    _enqueue_generation_fanout,
    _enqueue_mature_reflection_fanout,
    _enqueue_review_fanout,
    _enqueue_verification_fanout,
)
from co_scientist.orchestration.engine_tasks.gate import _apply_pre_ranking_evidence_gate
from co_scientist.orchestration.engine_tasks.inputs import _merge_scientist_inputs
from co_scientist.orchestration.engine_tasks.portfolio import _durable_queue_snapshot
from co_scientist.orchestration.engine_tasks.ranking import _schedule_ranking_chain
from co_scientist.orchestration.engine_tasks.support import (
    NODE_TASK_PREFIX,
    NodeCompletion,
    SupersededTaskError,
    TaskCommit,
    _emit_node_completion,
    _latest_task_checkpoint,
    _pause_node_task_if_requested,
    _save_paused_state,
    _save_state_and_enqueue,
    _successor_task_type,
    _task_commit,
)
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunRow, RunStatus, ScientificTask

if TYPE_CHECKING:
    from co_scientist.domains.research_state.state import WorkflowState

ADMISSION_NODE = "orchestrator"
_SYNC_FANOUT_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "review": _enqueue_review_fanout,
    "comprehensive_reflection": _enqueue_mature_reflection_fanout,
    "deep_verification": _enqueue_verification_fanout,
}


def _check_node_task_checkpoint(
    task: ScientificTask, checkpoint: dict[str, Any], current_seq: int
) -> dict[str, Any] | None:
    """Portfolio lookahead names its predecessor because its future
    checkpoint sequence is not known when queued.
    """
    if checkpoint["stage"] == f"engine_task:{task.id}":
        return {"checkpoint_seq": current_seq, "replayed": True}
    if task.dependencies:
        _check_portfolio_predecessor(task, checkpoint)
        return None
    expected_seq = int(task.inputs.get("checkpoint_seq", -1))
    if current_seq > expected_seq:
        raise SupersededTaskError("specialist task checkpoint was superseded")
    if current_seq != expected_seq:
        raise RuntimeError("specialist task checkpoint does not match input")
    return None


def _check_portfolio_predecessor(task: ScientificTask, checkpoint: dict[str, Any]) -> None:
    """A portfolio row can execute only when its predecessor committed it as
    the actual successor.
    """
    predecessor_id = task.dependencies[0]
    resume_successor = checkpoint.get("state", {}).get("resume_successor")
    stage = checkpoint["stage"]
    predecessor_stages = {
        f"engine_task:{predecessor_id}",
        f"engine_task_paused:{predecessor_id}",
    }
    if stage in predecessor_stages and resume_successor == task.task_type:
        return
    raise SupersededTaskError("portfolio task checkpoint was superseded")


def _restore_node_task_state(
    task: ScientificTask,
    checkpoint: dict[str, Any],
    generator: Any,
    opts: dict[str, Any],
    db_path: str | None,
) -> dict[str, Any]:
    """Only orchestration consumes steering; retryable synthesis failures
    propagate until the final attempt degrades to preserve report
    publication.
    """
    from co_scientist.orchestration.checkpoint import restore_workflow_state

    state: dict[str, Any] = restore_workflow_state(
        checkpoint["state"], tool_registry=generator.tool_registry
    )
    at_admission_node = task.task_type.removeprefix(NODE_TASK_PREFIX) == ADMISSION_NODE
    if opts.get("pending_steering") and at_admission_node:
        state["pending_steering"] = True
    if opts.get("preferences"):
        state["preferences"] = opts["preferences"]
    if opts.get("context_enrichment_sources"):
        state["context_enrichment_sources"] = opts["context_enrichment_sources"]
    state["durable_retries_remain"] = task.attempt < task.max_attempts
    _merge_scientist_inputs(
        state,
        task.run_id,
        db_path,
        admit_hypotheses=at_admission_node,
    )
    return state


def _prepare_node_task(
    task: ScientificTask,
    checkpoint: dict[str, Any],
    current_seq: int,
    db_path: str | None,
) -> tuple[dict[str, Any], TaskCommit, str]:
    """Only the orchestrator's own scheduling commit may acknowledge
    steering.
    """
    generator, opts = engine_tasks_runtime.active().generator_and_opts(task, db_path)
    state = _restore_node_task_state(task, checkpoint, generator, opts, db_path)
    node_name = task.task_type.removeprefix(NODE_TASK_PREFIX)
    if node_name == "orchestrator":
        state["durable_task_queue"] = _durable_queue_snapshot(task.run_id, db_path)
    commit = _task_commit(
        task,
        current_seq,
        db_path,
        opts,
        consume_steering=(node_name == ADMISSION_NODE),
    )
    return state, commit, node_name


async def _dispatch_node_fanout(
    task: ScientificTask,
    state: dict[str, Any],
    node_name: str,
    current_seq: int,
    db_path: str | None,
) -> dict[str, Any] | None:
    if node_name == "generate":
        return await _enqueue_generation_fanout(task, state, current_seq, db_path=db_path)
    if node_name == "ranking":
        await _apply_pre_ranking_evidence_gate(state)
        return await _schedule_ranking_chain(task, state, current_seq, db_path=db_path)
    handler = _SYNC_FANOUT_HANDLERS.get(node_name)
    if handler is None:
        return None
    return handler(task, state, current_seq, db_path=db_path)


async def _commit_node_result(
    commit: TaskCommit,
    run: RunRow,
    node_name: str,
    committed: dict[str, Any],
    successor: str | None,
) -> dict[str, Any]:
    """Pause requested during provider work is decided under the same
    transaction as the result checkpoint.
    """
    task, db_path = commit.task, commit.db_path
    successor_type = _successor_task_type(successor)
    if run.status == RunStatus.PAUSED.value:
        checkpoint_seq = _save_paused_state(commit, committed, successor_type)
        successor_id = None
    else:
        checkpoint_seq, successor_id = _save_state_and_enqueue(
            commit, committed, successor, pause_if_requested=True
        )
    await _emit_node_completion(
        task.run_id,
        NodeCompletion(node_name, successor, checkpoint_seq),
        committed,
        db_path,
    )
    if successor_id is None:
        return {
            "checkpoint_seq": checkpoint_seq,
            "node": node_name,
            "status": "paused",
        }
    return {
        "checkpoint_seq": checkpoint_seq,
        "successor_task_id": successor_id,
        "node": node_name,
    }


def _require_active_run(task: ScientificTask, db_path: str | None, *, stage: str) -> RunRow:
    run = runs.get_run(task.run_id, db_path=db_path)
    if run is None or run.status == RunStatus.CANCELLED.value:
        raise RuntimeError(f"run cancelled {stage}")
    return run


async def execute_node_task(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
    from co_scientist.orchestration.task_runtime import execute_task_node

    checkpoint, current_seq = _latest_task_checkpoint(task, db_path)
    run = _require_active_run(task, db_path, stage="before specialist run")
    replay = _check_node_task_checkpoint(task, checkpoint, current_seq)
    if replay is not None:
        return replay

    state, commit, node_name = _prepare_node_task(task, checkpoint, current_seq, db_path)
    paused = _pause_node_task_if_requested(commit, run, node_name, state)
    if paused is not None:
        return paused
    fanout = await _dispatch_node_fanout(task, state, node_name, current_seq, db_path=db_path)
    if fanout is not None:
        return fanout

    committed, successor = await execute_task_node(node_name, cast("WorkflowState", state))
    run = _require_active_run(task, db_path, stage="during specialist run")
    return await _commit_node_result(
        commit, run, node_name, cast("dict[str, Any]", committed), successor
    )
