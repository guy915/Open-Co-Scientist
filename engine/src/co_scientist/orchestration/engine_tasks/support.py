from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import co_scientist.orchestration.engine_tasks.runtime as engine_tasks_runtime
from co_scientist.core.run_modes import resolved_run_config
from co_scientist.domains.chat.repository import messages
from co_scientist.orchestration.engine_adapter.opts import (
    CONSUMED_STEERING_IDS_OPT,
    build_engine_opts,
    build_generator,
)
from co_scientist.orchestration.engine_tasks.portfolio import _enqueue_node_portfolio
from co_scientist.orchestration.repository import events, tasks
from co_scientist.orchestration.repository.tasks import NewTask
from co_scientist.orchestration.run_events import make_emitter
from co_scientist.platform import db
from co_scientist.platform.db import checkpoints as store
from co_scientist.platform.db import retrieval_calls as retrieval
from co_scientist.platform.db import runs
from co_scientist.platform.db.checkpoints import NewCheckpoint
from co_scientist.platform.db.models import (
    TERMINAL_STATUSES,
    RunRow,
    RunStatus,
    ScientificTask,
)


@dataclass(frozen=True)
class TaskCommit:
    """Only orchestrator commits acknowledge steering, atomically with the
    checkpoint that applied it.
    """

    task: ScientificTask
    current_seq: int
    db_path: str | None
    steering_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ExactSuccessor:
    """Successor idempotency uses task type and committed checkpoint
    sequence, making redelivery a no-op.
    """

    task_type: str
    inputs: dict[str, Any]
    idempotency_key: str


def _task_commit(
    task: ScientificTask,
    current_seq: int,
    db_path: str | None,
    opts: dict[str, Any],
    *,
    consume_steering: bool = False,
) -> TaskCommit:
    """Steering consumption defaults false; only the orchestrator's
    scheduling decision may retire pending input.
    """
    if not consume_steering:
        return TaskCommit(task, current_seq, db_path, ())
    consumed = opts.get(CONSUMED_STEERING_IDS_OPT) or []
    return TaskCommit(task, current_seq, db_path, tuple(int(item) for item in consumed))


def _ack_consumed_steering(
    commit: TaskCommit, conn: sqlite3.Connection, state: dict[str, Any]
) -> None:
    """Acknowledgement and the state honoring steering commit or roll back
    together.
    """
    messages.mark_steering_applied(
        list(commit.steering_ids), conn=conn, decision=state.get("next_task")
    )


def _performance_assessment(state: dict[str, Any]) -> dict[str, Any] | None:
    guidance = state.get("supervisor_guidance")
    if not isinstance(guidance, dict):
        return None
    assessment = guidance.get("performance_assessment")
    return assessment if isinstance(assessment, dict) and assessment else None


def _plain_metrics(state: dict[str, Any]) -> dict[str, Any]:
    metrics = state.get("metrics")
    if metrics is None:
        return {}
    if hasattr(metrics, "to_dict"):
        return dict(metrics.to_dict())
    return dict(metrics)


def _metrics_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Malformed or absent metrics must not fail a scientific checkpoint
    commit.
    """
    snapshot = _plain_metrics(state)
    assessment = _performance_assessment(state)
    if assessment is not None:
        snapshot["performance_assessment"] = assessment
    return snapshot


def merge_usage_snapshots(
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    from co_scientist.domains.research_state.models import (
        ExecutionMetrics,
        create_metrics_update,
        merge_metrics,
    )

    merged = ExecutionMetrics()
    for snapshot in snapshots:
        if not snapshot:
            continue
        merged = merge_metrics(merged, create_metrics_update(model_usage=dict(snapshot)))
    return merged.model_usage


@dataclass(frozen=True)
class NodeCompletion:
    node_name: str
    successor: str | None
    checkpoint_seq: int


def _plain_final_state(state: dict[str, Any]) -> dict[str, Any]:
    metrics = state.get("metrics")
    return {
        **state,
        "hypotheses": [item.to_dict() for item in state.get("hypotheses", [])],
        "articles": [item.to_dict() for item in state.get("articles") or []],
        "metrics": metrics.to_dict() if metrics else {},
    }


def _emit_node_milestone(
    run_id: str,
    node_name: str,
    state: dict[str, Any],
    db_path: str | None,
) -> None:
    """Events follow checkpoint replay guards; a crash may miss an event but
    redelivery must never duplicate it.
    """
    from co_scientist.orchestration.engine_adapter.events import (
        _MILESTONE_BUILDERS,
        _canonical_engine_payload,
        _canonical_event_type,
        append_node_milestone,
    )

    node_type = _canonical_event_type(node_name)
    if node_type not in _MILESTONE_BUILDERS:
        return
    payload = _canonical_engine_payload(node_name, node_type, _plain_final_state(state))
    append_node_milestone(run_id, node_type, payload, db_path=db_path)


async def _emit_node_completion(
    run_id: str,
    completion: NodeCompletion,
    committed: dict[str, Any],
    db_path: str | None,
) -> None:
    """Post-commit events pass replay guards; missing an event on crash is
    safer than duplicate effects on redelivery.
    """
    _emit_node_milestone(run_id, completion.node_name, committed, db_path)
    emit = make_emitter(run_id, db_path=db_path)
    await emit(
        "scientific_task",
        {
            "task": completion.node_name,
            "status": "completed",
            "checkpoint_seq": completion.checkpoint_seq,
            "successor": completion.successor,
        },
    )


def _save_paused_checkpoint(
    commit: TaskCommit,
    state: dict[str, Any],
    resume_successor: str,
    envelope: dict[str, Any],
    conn: sqlite3.Connection,
) -> int:
    from co_scientist.platform.db.checkpoints import CHECKPOINT_VERSION

    task = commit.task
    latest_seq = store.latest_checkpoint_seq(task.run_id, conn)
    if latest_seq != commit.current_seq:
        raise RuntimeError("checkpoint changed while pausing task")
    _ack_consumed_steering(commit, conn, state)
    retrieval.save_run_metrics(task.run_id, _metrics_snapshot(state), conn=conn)
    return store.save_checkpoint(
        task.run_id,
        NewCheckpoint(
            stage=f"engine_task_paused:{task.id}",
            schema_version=CHECKPOINT_VERSION,
            last_event_seq=envelope["last_event_seq"],
            state={
                "provider": _CHECKPOINT_PROVIDER,
                "resume_successor": resume_successor,
                **envelope,
            },
        ),
        conn=conn,
    )


def _save_paused_if_requested(
    commit: TaskCommit,
    state: dict[str, Any],
    resume_successor: str,
    envelope: dict[str, Any],
    conn: sqlite3.Connection,
) -> tuple[int, None] | None:
    task = commit.task
    run = conn.execute("SELECT status FROM runs WHERE id=?", (task.run_id,)).fetchone()
    if run is None or run["status"] != RunStatus.PAUSED.value:
        return None
    envelope["last_event_seq"] = events.latest_event_seq(task.run_id, conn=conn)
    return (
        _save_paused_checkpoint(commit, state, resume_successor, envelope, conn),
        None,
    )


def _save_paused_state(
    commit: TaskCommit,
    state: dict[str, Any],
    resume_successor: str,
) -> int:
    """Paused checkpoints still acknowledge applied steering atomically,
    without making successors claimable.
    """
    from co_scientist.orchestration.checkpoint import serialize_workflow_state

    task, db_path = commit.task, commit.db_path
    envelope = serialize_workflow_state(
        state,
        last_event_seq=events.latest_event_seq(task.run_id, db_path=db_path),
    )
    with db.transaction(db_path) as conn:
        assert_task_commit_allowed(task, conn)
        envelope["last_event_seq"] = events.latest_event_seq(task.run_id, conn=conn)
        return _save_paused_checkpoint(commit, state, resume_successor, envelope, conn)


def _save_paused_state_if_requested(
    commit: TaskCommit,
    state: dict[str, Any],
    resume_successor: str,
) -> int | None:
    from co_scientist.orchestration.checkpoint import serialize_workflow_state

    task, db_path = commit.task, commit.db_path
    envelope = serialize_workflow_state(state, last_event_seq=0)
    with db.transaction(db_path) as conn:
        assert_task_commit_allowed(task, conn)
        paused = _save_paused_if_requested(commit, state, resume_successor, envelope, conn)
        return paused[0] if paused is not None else None


def _pause_node_task_if_requested(
    commit: TaskCommit,
    run: RunRow,
    node_name: str,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    if run.status != RunStatus.PAUSED.value:
        return None
    checkpoint_seq = _save_paused_state(commit, state, commit.task.task_type)
    return {
        "checkpoint_seq": checkpoint_seq,
        "node": node_name,
        "status": "paused",
    }


ENGINE_TASK_PREFIX = "engine."
_CHECKPOINT_PROVIDER = "engine"
BOOTSTRAP_TASK = "engine.bootstrap"
NODE_TASK_PREFIX = "engine.node."
FINALIZE_TASK = "engine.finalize"
REVIEW_ITEM_TASK = "engine.fanout.review.item"
REVIEW_AGGREGATE_TASK = "engine.fanout.review.aggregate"
VERIFICATION_ITEM_TASK = "engine.fanout.verification.item"
VERIFICATION_AGGREGATE_TASK = "engine.fanout.verification.aggregate"
RANKING_MATCH_TASK = "engine.ranking.match"
RANKING_FINALIZE_TASK = "engine.ranking.finalize"
# Batch tournament progress to show liveness without per-match events crowding
# other stages out of the bounded feed.
RANKING_PROGRESS_EVERY = 5
GENERATION_STRATEGY_TASK = "engine.fanout.generation.strategy"
GENERATION_AGGREGATE_TASK = "engine.fanout.generation.aggregate"
MATURE_REFLECTION_ITEM_TASK = "engine.fanout.reflection.item"
MATURE_REFLECTION_AGGREGATE_TASK = "engine.fanout.reflection.aggregate"


class SupersededTaskError(RuntimeError):
    """A newer checkpoint makes the leased task obsolete."""


class SafetyHoldError(RuntimeError):
    """Held work is neither success nor failure; park the task so human
    approval can release the same boundary.
    """


def _require_run(task: ScientificTask, db_path: str | None) -> RunRow:
    run = runs.get_run(task.run_id, db_path=db_path)
    if run is None:
        raise RuntimeError(f"run {task.run_id} no longer exists")
    return run


def _require_item_task(item_id: Any, db_path: str | None, *, kind: str) -> ScientificTask:
    item = tasks.get_task(str(item_id), db_path=db_path)
    if item is None:
        raise RuntimeError(f"{kind} {item_id} disappeared")
    return item


def _successor_task_type(successor: str | None) -> str:
    if successor is None:
        return FINALIZE_TASK
    return f"{NODE_TASK_PREFIX}{successor}"


def assert_task_commit_allowed(task: ScientificTask, conn: sqlite3.Connection) -> None:
    """Lease and run liveness are checked inside the transaction that
    commits scientific effects.
    """
    row = conn.execute(
        "SELECT task.status AS task_status, task.lease_owner AS lease_owner, "
        "task.attempt AS attempt, "
        "run.status AS run_status FROM scientific_tasks AS task "
        "JOIN runs AS run ON run.id=task.run_id "
        "WHERE task.id=? AND task.run_id=?",
        (task.id, task.run_id),
    ).fetchone()
    terminal = {status.value for status in TERMINAL_STATUSES}
    if (
        task.status != "leased"
        or task.lease_owner is None
        or row is None
        or row["task_status"] != "leased"
        or row["lease_owner"] != task.lease_owner
        or row["attempt"] != task.attempt
        or row["run_status"] in terminal
    ):
        # Import below worker outcomes to avoid cycles; BEGIN IMMEDIATE fences
        # lease reads with checkpoint and successor writes.
        from co_scientist.orchestration.task_worker.outcomes import LeaseLostError

        raise LeaseLostError(
            f"task {task.id} cannot commit after lease revocation or run termination"
        )


def _generator_and_opts(task: ScientificTask, db_path: str | None) -> tuple[Any, dict[str, Any]]:
    from co_scientist.domains.access.credentials import get_run_credential
    from co_scientist.orchestration.generator.core import HypothesisGenerator

    run = _require_run(task, db_path)
    cfg = resolved_run_config(run.config)
    generator = build_generator(
        HypothesisGenerator,
        cfg,
        offline=runs.run_used_offline(run),
        byok=get_run_credential(task.run_id, db_path=db_path),
    )
    return generator, build_engine_opts(cfg, run.id, db_path)


def _generator_for_restore(task: ScientificTask, db_path: str | None) -> Any:
    from co_scientist.domains.access.credentials import get_run_credential
    from co_scientist.orchestration.generator.core import HypothesisGenerator

    run = _require_run(task, db_path)
    return build_generator(
        HypothesisGenerator,
        resolved_run_config(run.config),
        offline=runs.run_used_offline(run),
        byok=get_run_credential(task.run_id, db_path=db_path),
    )


def _save_node_checkpoint(
    task: ScientificTask,
    envelope: dict[str, Any],
    successor_type: str,
    expected_checkpoint_seq: int,
    conn: sqlite3.Connection,
) -> int:
    """Persist the exact resume successor; bootstrap recovery must not enter
    orchestration before supervisor guidance exists.
    """
    from co_scientist.platform.db.checkpoints import CHECKPOINT_VERSION

    latest_seq = store.latest_checkpoint_seq(task.run_id, conn)
    if latest_seq != expected_checkpoint_seq:
        raise RuntimeError("checkpoint changed while scientific task was executing")
    return store.save_checkpoint(
        task.run_id,
        NewCheckpoint(
            stage=f"engine_task:{task.id}",
            schema_version=CHECKPOINT_VERSION,
            last_event_seq=envelope["last_event_seq"],
            state={
                "provider": _CHECKPOINT_PROVIDER,
                "resume_successor": successor_type,
                **envelope,
            },
        ),
        conn=conn,
    )


def _save_state_and_enqueue(
    commit: TaskCommit,
    state: dict[str, Any],
    successor: str | None,
    *,
    pause_if_requested: bool = False,
) -> tuple[int, str | None]:
    """Checkpoint, successor and metrics commit together with one checkpoint
    advance; pause is decided under that same lock.
    """
    from co_scientist.orchestration.checkpoint import serialize_workflow_state

    task, db_path = commit.task, commit.db_path
    envelope = serialize_workflow_state(
        state,
        last_event_seq=events.latest_event_seq(task.run_id, db_path=db_path),
    )
    successor_type = _successor_task_type(successor)
    with db.transaction(db_path) as conn:
        assert_task_commit_allowed(task, conn)
        if pause_if_requested:
            paused = _save_paused_if_requested(commit, state, successor_type, envelope, conn)
            if paused is not None:
                return paused
        checkpoint_seq = _save_node_checkpoint(
            task, envelope, successor_type, commit.current_seq, conn
        )
        successor_task = _enqueue_node_portfolio(task, state, successor, successor_type, conn)
        _ack_consumed_steering(commit, conn, state)
        retrieval.save_run_metrics(task.run_id, _metrics_snapshot(state), conn=conn)
    return checkpoint_seq, successor_task.id


def _save_exact_checkpoint(
    task: ScientificTask,
    envelope: dict[str, Any],
    expected_checkpoint_seq: int,
    conn: sqlite3.Connection,
    *,
    changed_message: str = "checkpoint changed during scientific task",
) -> int:
    from co_scientist.platform.db.checkpoints import CHECKPOINT_VERSION

    latest_seq = store.latest_checkpoint_seq(task.run_id, conn)
    if latest_seq != expected_checkpoint_seq:
        raise RuntimeError(changed_message)
    return store.save_checkpoint(
        task.run_id,
        NewCheckpoint(
            stage=f"engine_task:{task.id}",
            schema_version=CHECKPOINT_VERSION,
            last_event_seq=envelope["last_event_seq"],
            state={"provider": _CHECKPOINT_PROVIDER, **envelope},
        ),
        conn=conn,
    )


def _enqueue_exact_successor(
    task: ScientificTask,
    successor: ExactSuccessor,
    checkpoint_seq: int,
    conn: sqlite3.Connection,
) -> ScientificTask:
    inputs = {**successor.inputs, "checkpoint_seq": checkpoint_seq}
    return tasks.enqueue_task(
        NewTask(
            run_id=task.run_id,
            task_type=successor.task_type,
            inputs=inputs,
            idempotency_key=successor.idempotency_key.format(checkpoint_seq=checkpoint_seq),
            priority=86,
            dependencies=(task.id,),
            provenance={"scheduled_by": task.task_type},
        ),
        conn=conn,
    )


def _save_state_and_enqueue_exact(
    commit: TaskCommit,
    state: dict[str, Any],
    successor: ExactSuccessor,
) -> tuple[int, str]:
    """Non-node effect checkpoints and their exact successor enqueue commit
    atomically.
    """
    from co_scientist.orchestration.checkpoint import serialize_workflow_state

    task = commit.task
    envelope = serialize_workflow_state(
        state,
        last_event_seq=events.latest_event_seq(task.run_id, db_path=commit.db_path),
    )
    with db.transaction(commit.db_path) as conn:
        assert_task_commit_allowed(task, conn)
        checkpoint_seq = _save_exact_checkpoint(task, envelope, commit.current_seq, conn)
        enqueued = _enqueue_exact_successor(task, successor, checkpoint_seq, conn)
    return checkpoint_seq, enqueued.id


def _latest_task_checkpoint(
    task: ScientificTask, db_path: str | None
) -> tuple[dict[str, Any], int]:
    checkpoint = store.get_latest_checkpoint(task.run_id, db_path=db_path)
    if checkpoint is None:
        raise RuntimeError("specialist task has no workflow checkpoint")
    return checkpoint, int(checkpoint["seq"])


def _replay_or_supersede(
    task: ScientificTask,
    db_path: str | None,
    *,
    label: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], int]:
    """Replay returns before duplicate effects; otherwise execution requires
    the leased checkpoint to remain current.
    """
    checkpoint, current_seq = _latest_task_checkpoint(task, db_path)
    expected_seq = int(task.inputs["checkpoint_seq"])
    if current_seq > expected_seq and checkpoint["stage"] == f"engine_task:{task.id}":
        return (
            {"checkpoint_seq": current_seq, "replayed": True},
            checkpoint,
            current_seq,
        )
    if current_seq != expected_seq:
        raise SupersededTaskError(f"{label} checkpoint was superseded")
    return None, checkpoint, current_seq


def restore_checkpoint_state(
    task: ScientificTask, checkpoint: dict[str, Any], db_path: str | None
) -> dict[str, Any]:
    """Restore builds and drops its registry-compatible generator within the
    cohort's own event loop, without consuming steering.
    """
    from co_scientist.orchestration.checkpoint import restore_workflow_state

    generator = engine_tasks_runtime.active().generator_for_restore(task, db_path)
    return restore_workflow_state(checkpoint["state"], tool_registry=generator.tool_registry)


def leased_state(
    task: ScientificTask, db_path: str | None, *, label: str
) -> tuple[dict[str, Any] | None, dict[str, Any], int]:
    """A replay result returns immediately before restoring or repeating
    scientific work.
    """
    replay, checkpoint, current_seq = _replay_or_supersede(task, db_path, label=label)
    if replay is not None:
        return replay, {}, current_seq
    return (
        None,
        restore_checkpoint_state(task, checkpoint, db_path),
        current_seq,
    )


def _restore_item_checkpoint(
    task: ScientificTask, db_path: str | None, *, superseded: str
) -> tuple[dict[str, Any], int]:
    """Items restore immutable plan state only while their leased checkpoint
    remains current.
    """
    checkpoint, current_seq = _latest_task_checkpoint(task, db_path)
    expected_seq = int(task.inputs["checkpoint_seq"])
    if current_seq != expected_seq:
        raise SupersededTaskError(f"{superseded} checkpoint was superseded")
    return restore_checkpoint_state(task, checkpoint, db_path), expected_seq
