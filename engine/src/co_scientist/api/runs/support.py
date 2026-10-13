"""Existence guards live below routers to avoid sibling/router import cycles."""

from __future__ import annotations

from fastapi import HTTPException

from co_scientist.core.exceptions import ContinuationAdmissionError
from co_scientist.domains.research_state.repository import records
from co_scientist.orchestration import engine_adapter
from co_scientist.orchestration.repository import events, tasks
from co_scientist.platform.db import Connection, checkpoints, current_time, runs
from co_scientist.platform.db.models import RunRow, RunStatus, ScientificTask
from co_scientist.platform.llm.routing import RunReadmission


def _run_or_404(run_id: str, conn: Connection | None = None) -> RunRow:
    run = runs.get_run(run_id, conn=conn)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def can_resume_interrupted_run(run: RunRow, conn: Connection | None = None) -> bool:
    event = events.latest_status_event(run.id, conn=conn) or {}
    saved_tasks = tasks.list_tasks(run.id, conn=conn)
    recovery_kind = event.get("failure_kind")
    provider_rejected = recovery_kind == "llm_credit_exhausted"
    paused_boundary = any(
        task.status == "paused" and task.task_type.startswith("engine.") for task in saved_tasks
    )
    failed_bootstrap = provider_rejected and any(
        task.status == "failed" and task.task_type == "engine.bootstrap" for task in saved_tasks
    )
    interrupted = (run.status == RunStatus.PAUSED.value and paused_boundary) or (
        run.status == RunStatus.FAILED.value
        and event.get("status") == run.status
        and recovery_kind in {"llm_timeout_unknown", "llm_timeout", "llm_credit_exhausted"}
        and (
            engine_adapter.is_engine_checkpoint(
                checkpoints.get_latest_checkpoint(run.id, conn=conn)
            )
            or failed_bootstrap
        )
    )
    if not interrupted:
        return False
    if records.count_unresolved_review_decisions(run.id, conn=conn):
        return False
    return not any(
        task.status == "leased"
        and (task.lease_expires_at is None or task.lease_expires_at > current_time())
        for task in saved_tasks
    )


def resume_requires_ack(run: RunRow, conn: Connection | None = None) -> bool:
    event = events.latest_status_event(run.id, conn=conn) or {}
    return event.get("failure_kind") in {"llm_timeout_unknown", "llm_interrupted_unknown"} or any(
        task.status == "leased" for task in tasks.list_tasks(run.id, conn=conn)
    )


def _require_run(run_id: str) -> None:
    if not runs.run_exists(run_id):
        raise HTTPException(status_code=404, detail="run not found")


def prepare_continuation(run_id: str) -> RunReadmission | None:
    from co_scientist.orchestration.engine_tasks.inputs import prepare_scientist_continuation

    try:
        return prepare_scientist_continuation(run_id)
    except ContinuationAdmissionError as exc:
        raise HTTPException(status_code=409 if exc.capacity else 429, detail=str(exc)) from exc


def request_continuation(
    run_id: str,
    input_id: int,
    *,
    conn: Connection | None = None,
    admission: RunReadmission | None = None,
) -> ScientificTask | None:
    from co_scientist.orchestration import engine_tasks

    try:
        return engine_tasks.enqueue_scientist_continuation(
            run_id, input_id, conn=conn, admission=admission
        )
    except ContinuationAdmissionError as exc:
        raise HTTPException(status_code=409 if exc.capacity else 429, detail=str(exc)) from exc
