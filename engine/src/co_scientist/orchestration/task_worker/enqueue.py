from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from co_scientist.orchestration import engine_tasks
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.orchestration.repository.tasks import NewTask
from co_scientist.orchestration.repository.tasks_lifecycle import _DEAD_LEASE_ERROR
from co_scientist.platform import db as store_db
from co_scientist.platform.db import checkpoints
from co_scientist.platform.db.models import ScientificTask

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResumeDB:
    path: str | None
    conn: sqlite3.Connection | None = None


@dataclass(frozen=True)
class _CheckpointMatch:
    seq: int
    successor: str
    predecessor_id: str | None
    fanout_source: str | None
    include_predecessor_lease: bool


# Match the abandonment marker, not exhaustion alone, which can also signal a
# permanent failure.
def is_abandoned_spent_bootstrap(task: ScientificTask) -> bool:
    return (
        task.task_type == engine_tasks.BOOTSTRAP_TASK
        and task.status == "failed"
        and task.attempt >= task.max_attempts
        and task.error == _DEAD_LEASE_ERROR
    )


def _resume_predecessor_id(
    checkpoint: dict[str, Any],
    db: _ResumeDB,
) -> str | None:
    """Validate checkpoint predecessor IDs against actual tasks; a plausible
    but nonexistent dependency would wedge resume forever.
    """
    stage = str(checkpoint["stage"])
    if not (stage.startswith("engine_task:") or stage.startswith("engine_task_paused:")):
        return None
    candidate = stage.rsplit(":", 1)[-1]
    return candidate if store.get_task(candidate, db_path=db.path, conn=db.conn) else None


def _enqueue_resume_task(
    run_id: str,
    checkpoint: dict[str, Any],
    db: _ResumeDB,
) -> ScientificTask:
    """Resume and portfolio enqueue share predecessor-based edge keys;
    legacy checkpoints retain sequence-based fallback.
    """
    checkpoint_seq = int(checkpoint["seq"])
    recorded_successor = checkpoint["state"].get("resume_successor")
    task_type = str(recorded_successor or "") or (f"{engine_tasks.NODE_TASK_PREFIX}orchestrator")
    predecessor_id = _resume_predecessor_id(checkpoint, db)
    anchored = bool(recorded_successor and predecessor_id is not None)
    idempotency_key = (
        f"{task_type}:after:{predecessor_id}" if anchored else f"{task_type}:{checkpoint_seq}"
    )
    _revive_dead_resume_target(run_id, task_type, idempotency_key, db)
    return store.enqueue_task(
        NewTask(
            run_id=run_id,
            task_type=task_type,
            inputs={"checkpoint_seq": checkpoint_seq},
            idempotency_key=idempotency_key,
            priority=100,
            dependencies=(predecessor_id,) if anchored and predecessor_id else (),
            provenance={"scheduled_by": "resume"},
        ),
        db_path=db.path,
        conn=db.conn,
    )


def _revive_dead_resume_target(
    run_id: str,
    task_type: str,
    idempotency_key: str,
    db: _ResumeDB,
) -> None:
    """A dead boundary retains its idempotency key; revive it before enqueue
    or the resume would create no work.
    """
    if lifecycle.revive_task_for_retry(run_id, idempotency_key, db_path=db.path, conn=db.conn):
        logger.info("Resume revived a dead %s task for run %s", task_type, run_id)


def _is_live_engine_resume_row(task: ScientificTask, now: float) -> bool:
    if not task.task_type.startswith(engine_tasks.ENGINE_TASK_PREFIX):
        return False
    if task.status == "queued":
        return True
    return task.status == "leased" and (
        task.lease_expires_at is None
        or task.lease_expires_at > now
        or task.attempt < task.max_attempts
    )


def _matches_checkpoint_target(
    task: ScientificTask,
    checkpoint_seq: int,
    successor: str,
    predecessor_id: str | None,
    *,
    include_predecessor_lease: bool = True,
) -> bool:
    if include_predecessor_lease and task.id == predecessor_id and task.status == "leased":
        return True
    if not successor or task.task_type != successor:
        return False
    if predecessor_id is None:
        return task.inputs.get("checkpoint_seq") == checkpoint_seq
    return predecessor_id in task.dependencies


def _is_checkpoint_fanout_row(
    task: ScientificTask,
    checkpoint_seq: int,
    predecessor_type: str | None,
) -> bool:
    return (
        predecessor_type is not None
        and task.inputs.get("checkpoint_seq") == checkpoint_seq
        and task.provenance.get("scheduled_by") == predecessor_type
    )


def _revive_expired_checkpoint_writer(
    run_id: str,
    predecessor: ScientificTask | None,
    now: float,
    db: _ResumeDB,
) -> None:
    if (
        predecessor is None
        or predecessor.status != "leased"
        or predecessor.lease_expires_at is None
        or predecessor.lease_expires_at > now
    ):
        return
    if lifecycle.revive_task_for_retry(
        run_id,
        predecessor.idempotency_key,
        db_path=db.path,
        conn=db.conn,
    ):
        logger.info(
            "Resume revived expired checkpoint writer %s for run %s",
            predecessor.task_type,
            run_id,
        )


def _matching_checkpoint_rows(
    tasks: list[ScientificTask],
    match: _CheckpointMatch,
    now: float,
) -> list[ScientificTask]:
    return [
        task
        for task in tasks
        if _is_live_engine_resume_row(task, now)
        and (
            _matches_checkpoint_target(
                task,
                match.seq,
                match.successor,
                match.predecessor_id,
                include_predecessor_lease=match.include_predecessor_lease,
            )
            or _is_checkpoint_fanout_row(task, match.seq, match.fanout_source)
        )
    ]


def _find_checkpoint_resume_task(
    run_id: str,
    tasks: list[ScientificTask],
    checkpoint: dict[str, Any],
    db: _ResumeDB,
) -> ScientificTask | None:
    checkpoint_seq = int(checkpoint["seq"])
    predecessor_id = _resume_predecessor_id(checkpoint, db)
    predecessor = (
        store.get_task(predecessor_id, db_path=db.path, conn=db.conn)
        if predecessor_id is not None
        else None
    )
    if predecessor is not None and predecessor.run_id != run_id:
        predecessor = None
        predecessor_id = None
    successor = str(checkpoint["state"].get("resume_successor") or "")
    fanout_source = successor or (predecessor.task_type if predecessor is not None else None)
    now = time.time()
    _revive_expired_checkpoint_writer(run_id, predecessor, now, db)
    paused_stage = str(checkpoint["stage"]).startswith("engine_task_paused:")
    match = _CheckpointMatch(
        checkpoint_seq,
        successor,
        predecessor_id,
        fanout_source,
        include_predecessor_lease=not paused_stage,
    )
    matching = _matching_checkpoint_rows(tasks, match, now)
    queued = next((task for task in matching if task.status == "queued"), None)
    return queued or next(iter(matching), None)


def _already_claimable_task(
    run_id: str,
    db: _ResumeDB,
) -> ScientificTask | None:
    """Pause may race successor enqueue; locate continuation using current
    checkpoint and predecessor, never a stale portfolio guess.
    """
    unpaused = lifecycle.resume_run_tasks(run_id, db_path=db.path, conn=db.conn)
    checkpoint = checkpoints.get_latest_checkpoint(run_id, db_path=db.path, conn=db.conn)
    tasks = store.list_tasks(run_id, db_path=db.path, conn=db.conn)

    if checkpoint is None:
        # Safety holds may park before the first checkpoint; preserve that
        # resume boundary.
        return next(
            (
                task
                for task in tasks
                if unpaused
                and task.status == "queued"
                and task.task_type.startswith(engine_tasks.ENGINE_TASK_PREFIX)
            ),
            None,
        )

    return _find_checkpoint_resume_task(run_id, tasks, checkpoint, db)


def _enqueue_resumed_workflow(
    run_id: str,
    db: _ResumeDB,
    *,
    revive_failed_precheckpoint_bootstrap: bool = False,
) -> ScientificTask:
    existing = _already_claimable_task(run_id, db)
    if existing is not None:
        return existing
    checkpoint = checkpoints.get_latest_checkpoint(run_id, db_path=db.path, conn=db.conn)
    if checkpoint is not None:
        return _enqueue_resume_task(run_id, checkpoint, db)
    _revive_resumable_precheckpoint_bootstrap(
        run_id,
        db,
        allow_failed=revive_failed_precheckpoint_bootstrap,
    )
    return engine_tasks.enqueue_bootstrap(run_id, db_path=db.path, conn=db.conn)


def _revive_resumable_precheckpoint_bootstrap(
    run_id: str,
    db: _ResumeDB,
    *,
    allow_failed: bool = False,
) -> None:
    bootstrap = next(
        (
            task
            for task in store.list_tasks(run_id, db_path=db.path, conn=db.conn)
            if task.task_type == engine_tasks.BOOTSTRAP_TASK
        ),
        None,
    )
    now = time.time()
    if (
        bootstrap is None
        or (
            bootstrap.status == "leased"
            and (bootstrap.lease_expires_at is None or bootstrap.lease_expires_at > now)
        )
        or bootstrap.status not in {"leased", "failed"}
        or (bootstrap.status == "failed" and not allow_failed)
    ):
        return
    _revive_dead_resume_target(
        run_id,
        bootstrap.task_type,
        bootstrap.idempotency_key,
        db,
    )


def enqueue_run_workflow(
    run_id: str,
    *,
    resume: bool = False,
    revive_failed_precheckpoint_bootstrap: bool = False,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> ScientificTask:
    """Resume discovery and enqueue share the lifecycle write transaction to
    avoid raced duplicate admission.
    """
    if resume:
        if conn is not None:
            return _enqueue_resumed_workflow(
                run_id,
                _ResumeDB(db_path, conn),
                revive_failed_precheckpoint_bootstrap=revive_failed_precheckpoint_bootstrap,
            )
        with store_db.transaction(db_path) as conn:
            return _enqueue_resumed_workflow(
                run_id,
                _ResumeDB(db_path, conn),
                revive_failed_precheckpoint_bootstrap=revive_failed_precheckpoint_bootstrap,
            )
    return engine_tasks.enqueue_bootstrap(run_id, db_path=db_path, conn=conn)
