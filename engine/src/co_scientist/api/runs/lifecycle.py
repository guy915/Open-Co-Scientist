from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

import co_scientist.orchestration.engine_adapter as engine_adapter
import co_scientist.orchestration.engine_tasks as engine_tasks
import co_scientist.orchestration.task_worker as task_worker
from co_scientist.api.operator_access import has_admin_token
from co_scientist.api.runs.models import (
    ResumeRunRequest,
    SafetyAdjudicationRequest,
    StartRunRequest,
)
from co_scientist.api.runs.support import (
    _run_or_404,
    can_resume_interrupted_run,
    resume_requires_ack,
)
from co_scientist.core.async_bridge import off_loop
from co_scientist.core.config import settings
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.domains.research_state.repository import records
from co_scientist.orchestration.repository import events, tasks
from co_scientist.orchestration.repository import runs_views as views
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.orchestration.task_worker.enqueue import is_abandoned_spent_bootstrap
from co_scientist.platform import db
from co_scientist.platform.db import Connection, checkpoints, runs
from co_scientist.platform.db.models import (
    TERMINAL_STATUSES,
    RunRow,
    RunStatus,
    ScientificTask,
)
from co_scientist.platform.llm.routing import prepare_readmission, recheck_readmission

logger = logging.getLogger(__name__)


def _has_leased_precheckpoint_bootstrap(run_id: str, *, conn: Connection | None = None) -> bool:
    return not checkpoints.has_checkpoint(run_id, conn=conn) and lifecycle.has_task_of_type(
        run_id, engine_tasks.BOOTSTRAP_TASK, status="leased", conn=conn
    )


def _has_failed_precheckpoint_bootstrap_while_paused(
    run_id: str, *, conn: Connection | None = None
) -> bool:
    if checkpoints.has_checkpoint(run_id, conn=conn):
        return False
    run = runs.get_run(run_id, conn=conn)
    if run is None or run.status != RunStatus.PAUSED.value:
        return False
    return any(is_abandoned_spent_bootstrap(task) for task in tasks.list_tasks(run_id, conn=conn))


def _has_recoverable_failed_bootstrap(run_id: str, *, conn: Connection | None = None) -> bool:
    run = runs.get_run(run_id, conn=conn)
    return bool(
        run
        and run.status == RunStatus.FAILED.value
        and not checkpoints.has_checkpoint(run_id, conn=conn)
        and can_resume_interrupted_run(run, conn=conn)
    )


def lifecycle_revision(run_id: str, *, conn: Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM run_events "
        "WHERE run_id=? AND type IN ('status', 'lifecycle')",
        (run_id,),
    ).fetchone()
    return int(row[0])


def resume_admission_snapshot(run_id: str) -> tuple[RunRow, int]:
    with db.connect() as conn:
        conn.execute("BEGIN")
        run = _run_or_404(run_id, conn=conn)
        revision = lifecycle_revision(run_id, conn=conn)
        conn.execute("COMMIT")
    return run, revision


def _prepare_resume_state(run_id: str, *, conn: Connection | None = None) -> bool:
    """True engine resume retains committed artifacts and events; legacy
    envelopes clear derived data before fresh bootstrap.
    """
    checkpoint = checkpoints.get_latest_checkpoint(run_id, conn=conn)
    true_resume = (
        engine_adapter.is_engine_checkpoint(checkpoint)
        or lifecycle.has_task_of_type(
            run_id, engine_tasks.ENGINE_TASK_PREFIX, status="paused", conn=conn
        )
        or _has_leased_precheckpoint_bootstrap(run_id, conn=conn)
        or _has_failed_precheckpoint_bootstrap_while_paused(run_id, conn=conn)
        or _has_recoverable_failed_bootstrap(run_id, conn=conn)
    )
    if not true_resume:
        # Retain a sequence above every discarded event, even when the
        # checkpoint floor
        # predates trailing log rows.
        events.append_event(
            run_id,
            "lifecycle",
            {"event": "legacy_resume_cleanup"},
            conn=conn,
        )
        views.clear_run_derived_data(run_id, conn=conn)
        checkpoints.clear_checkpoints(run_id, conn=conn)
    return true_resume


def _resume_detail(true_resume: bool) -> str:
    return "from specialist checkpoint" if true_resume else "from checkpoint"


def _check_resume_admission(
    run_id: str,
    expected_status: str,
    expected_lifecycle_revision: int,
    *,
    conn: Connection,
) -> None:
    run = _run_or_404(run_id, conn=conn)
    if (
        run.status != expected_status
        or lifecycle_revision(run_id, conn=conn) != expected_lifecycle_revision
    ):
        raise HTTPException(status_code=409, detail="run status changed while resuming")


def _queue_resume_workflow(
    run_id: str,
    *,
    expected_status: str,
    expected_lifecycle_revision: int,
    acknowledge_interrupted: bool = False,
) -> ScientificTask:
    before = _run_or_404(run_id)
    try:
        admission = prepare_readmission(run_id, before.research_goal, before.config)
    except ProviderAdmissionError as error:
        raise HTTPException(503, str(error)) from error
    with db.transaction() as conn:
        _check_resume_admission(
            run_id,
            expected_status,
            expected_lifecycle_revision,
            conn=conn,
        )
        run = _run_or_404(run_id, conn=conn)
        from co_scientist.platform.db.launch_control import require_unpaused

        if acknowledge_interrupted:
            require_unpaused(conn=conn)
            if not can_resume_interrupted_run(run, conn=conn):
                raise HTTPException(409, "run recovery state changed while resuming")
            lifecycle.prepare_interrupted_replay(run_id, conn=conn)
        try:
            recheck_readmission(conn, run_id, admission)
        except ProviderAdmissionError as error:
            raise HTTPException(503, str(error)) from error
        if not runs.has_run_capacity_in_transaction(
            conn, run.id, run.client_id, settings.max_concurrent_runs
        ):
            raise HTTPException(status_code=409, detail="concurrent run limit reached")
        revive_failed_bootstrap = _has_recoverable_failed_bootstrap(
            run_id, conn=conn
        ) or _has_failed_precheckpoint_bootstrap_while_paused(run_id, conn=conn)
        true_resume = _prepare_resume_state(run_id, conn=conn)
        runs.update_run_status(run_id, RunStatus.QUEUED, conn=conn)
        events.append_event(
            run_id,
            "status",
            {"status": "resuming", "detail": _resume_detail(true_resume)},
            conn=conn,
        )
        queued = task_worker.enqueue_run_workflow(
            run_id,
            resume=true_resume,
            revive_failed_precheckpoint_bootstrap=revive_failed_bootstrap,
            conn=conn,
        )
    logger.info(
        "Resume for run %s landed on %s task %s (status=%s)",
        run_id,
        queued.task_type,
        queued.id[:8],
        queued.status,
    )
    return queued


async def _apply_adjudication_lifecycle(
    run: RunRow,
    decision: dict[str, Any],
    resolution: str,
    *,
    expected_lifecycle_revision: int,
) -> None:
    """Whole-run holds affect lifecycle; a rejected hypothesis hold confirms
    exclusion without blocking the run.
    """
    if decision["stage"] == "hypothesis":
        return
    if resolution == "rejected":
        _block_rejected_run_if_current(run, expected_lifecycle_revision=expected_lifecycle_revision)
    elif run.status == RunStatus.PAUSED.value:
        event = events.latest_status_event(run.id) or {}
        if event.get("failure_kind") in {"llm_credit_exhausted", "llm_interrupted_unknown"}:
            return
        # Approval releases the parked boundary through resume; approved stages
        # must not be screened into another hold.
        await _launch_resume(
            run.id,
            expected_status=run.status,
            expected_lifecycle_revision=expected_lifecycle_revision,
        )


def _block_rejected_run_if_current(run: RunRow, *, expected_lifecycle_revision: int) -> None:
    with db.transaction() as conn:
        current = runs.get_run(run.id, conn=conn)
        if current is None:
            raise HTTPException(status_code=404, detail="run not found")
        if (
            current.status != run.status
            or lifecycle_revision(run.id, conn=conn) != expected_lifecycle_revision
        ):
            raise HTTPException(status_code=409, detail="run status changed during adjudication")
        runs.update_run_status(
            run.id,
            RunStatus.BLOCKED,
            error="Safety reviewer rejected held content.",
            conn=conn,
        )


async def adjudicate_safety(
    run_id: str,
    decision_id: int,
    body: SafetyAdjudicationRequest,
    request: Request,
) -> dict[str, Any]:
    """Resolve one held safety decision and update the run lifecycle."""
    if not has_admin_token(request):
        raise HTTPException(status_code=403, detail="an operator token is required")
    run, revision = resume_admission_snapshot(run_id)
    resolved = records.resolve_safety_decision(run_id, decision_id, body.resolution, "operator")
    if not resolved:
        raise HTTPException(
            status_code=409,
            detail="decision is not reviewable or was already resolved",
        )
    decisions = records.list_safety_decisions(run_id)
    decision = next(item for item in decisions if item["id"] == decision_id)
    await _apply_adjudication_lifecycle(
        run,
        decision,
        body.resolution,
        expected_lifecycle_revision=revision,
    )
    return {"resolution": body.resolution, "decision_id": decision_id}


# Detached resume tasks need strong references until they complete.
_resume_tasks: set[asyncio.Task[None]] = set()
router = APIRouter()


def _check_startable(run: RunRow) -> None:
    if run.status in (
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.SYNTHESIZING,
    ):
        raise HTTPException(status_code=409, detail="run already in progress")
    if run.status == RunStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="run already completed")


def _reserve_capacity_or_409(run: RunRow, conn: Any) -> None:
    """All tiers share one concurrent-run allowance; provider spend is
    bounded separately by tier budgets.
    """
    limit = settings.max_concurrent_runs
    if not runs.reserve_run_capacity_in_transaction(
        conn,
        run.id,
        run.client_id,
        limit,
        run.status,
    ):
        current = runs.get_run(run.id, conn=conn)
        if current is None:
            raise HTTPException(status_code=404, detail="run not found")
        if current.status != run.status:
            raise HTTPException(status_code=409, detail="run status changed while starting")
        raise HTTPException(
            status_code=409,
            detail=f"concurrent run limit reached ({limit})",
        )


def _enqueue_workflow_and_maybe_launch_worker(
    run: RunRow, background: BackgroundTasks
) -> ScientificTask:
    try:
        admission = prepare_readmission(run.id, run.research_goal, run.config)
    except ProviderAdmissionError as error:
        raise HTTPException(503, str(error)) from error
    with db.transaction() as conn:
        from co_scientist.platform.db.launch_control import require_unpaused

        require_unpaused(conn=conn)
        try:
            recheck_readmission(conn, run.id, admission)
        except ProviderAdmissionError as error:
            raise HTTPException(503, str(error)) from error
        _reserve_capacity_or_409(run, conn)
        lifecycle.revive_task_for_retry(
            run.id,
            "engine:bootstrap:v1",
            conn=conn,
        )
        task = engine_tasks.enqueue_bootstrap(run.id, conn=conn)
        events.append_event(
            run.id,
            "lifecycle",
            {"event": "queued"},
            conn=conn,
        )
    background.add_task(
        task_worker.run_run_worker_pool_sync,
        run.id,
        f"embedded-api:{os.getpid()}",
    )
    return task


@router.post("/{run_id}/start")
@off_loop
def start_run(run_id: str, req: StartRunRequest, background: BackgroundTasks) -> dict[str, Any]:
    """Queue a run and launch its workflow through the durable worker.

    Args:
        run_id: Path identifier of the run to start.
        req: Request body (empty; kept to preserve the endpoint's body
            contract for existing clients).
        background: FastAPI background task registry for the embedded-worker
            compatibility mode.

    Returns:
        A dict with the run 'id', its new 'status', and the enqueued task id.

    Raises:
        HTTPException: If the run is missing, already in progress, already
            completed, or the client's concurrent-run limit is reached.
    """
    run = _run_or_404(run_id)
    _check_startable(run)
    if run.status in {
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    } and checkpoints.has_checkpoint(run_id):
        raise HTTPException(
            status_code=409,
            detail="run has a checkpoint and cannot be restarted",
        )
    if run.status == RunStatus.BLOCKED.value:
        raise HTTPException(
            status_code=409,
            detail="run was blocked; create a new run",
        )
    task = _enqueue_workflow_and_maybe_launch_worker(run, background)
    return {"id": run_id, "status": "queued", "task_id": task.id}


@router.post("/{run_id}/resume")
@off_loop
def resume_run(run_id: str, req: ResumeRunRequest, background: BackgroundTasks) -> dict[str, Any]:
    run, revision = resume_admission_snapshot(run_id)
    if not can_resume_interrupted_run(run):
        raise HTTPException(409, "run has no interrupted provider work to resume")
    if resume_requires_ack(run) and not req.acknowledge_unknown_outcome:
        raise HTTPException(
            409, "resuming may repeat provider requests and, depending on the route, charges"
        )
    task = _queue_resume_workflow(
        run_id,
        expected_status=run.status,
        expected_lifecycle_revision=revision,
        acknowledge_interrupted=True,
    )
    background.add_task(
        task_worker.run_run_worker_pool_sync, run_id, f"embedded-resume:{os.getpid()}"
    )
    return {"id": run_id, "status": "queued", "task_id": task.id}


@router.post("/{run_id}/cancel")
@off_loop
def cancel_run(run_id: str) -> dict[str, Any]:
    """Cancel a run by revoking its durable tasks and marking it CANCELLED.

    Cancellation is durable, not in-process: revoking the run's queued and
    leased tasks is what stops the workers (their lease heartbeat observes
    the revocation and cancels in-flight work), so any non-terminal run is
    transitioned to CANCELLED here directly and a terminal ``cancelled``
    status event is emitted (mirroring the failed-run path) so open SSE
    streams close. An already-terminal run cannot be cancelled, returns 409.
    """
    with db.transaction() as conn:
        run = runs.get_run(run_id, conn=conn)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if RunStatus(run.status) in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="run already finished")
        lifecycle.cancel_run_tasks(run_id, conn=conn)
        runs.update_run_status(run_id, RunStatus.CANCELLED, conn=conn)
        events.append_event(run_id, "status", {"status": "cancelled"}, conn=conn)
    return {"id": run_id, "status": "cancelled"}


def _log_resume_task_result(task: asyncio.Task[None]) -> None:
    """Detached worker exceptions must be retrieved and logged rather than
    silently stopping a run.
    """
    _resume_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Resume worker crashed", exc_info=task.exception())


def _launch_embedded_resume_worker(run_id: str) -> None:
    """Worker serialization and SQLite writes run off the API event loop;
    resume uses the same cohort parallelism as start.
    """
    task = asyncio.create_task(
        asyncio.to_thread(
            task_worker.run_run_worker_pool_sync,
            run_id,
            f"embedded-resume:{os.getpid()}",
        )
    )
    _resume_tasks.add(task)
    task.add_done_callback(_log_resume_task_result)


async def _launch_resume(
    run_id: str,
    *,
    expected_status: str | None = None,
    expected_lifecycle_revision: int | None = None,
) -> None:
    """Detached resume tasks need strong references until completion,
    including startup resumes outside request scope.
    """
    if expected_status is None or expected_lifecycle_revision is None:
        # Direct callers retain their contract; request, hold and startup paths
        # pass the
        # state observed at admission.
        run, revision = resume_admission_snapshot(run_id)
        if expected_status is None:
            expected_status = run.status
        if expected_lifecycle_revision is None:
            expected_lifecycle_revision = revision
    _queue_resume_workflow(
        run_id,
        expected_status=expected_status,
        expected_lifecycle_revision=expected_lifecycle_revision,
    )
    _launch_embedded_resume_worker(run_id)


async def _resume_interrupted_run(run_id: str) -> None:
    try:
        run, lifecycle_revision = resume_admission_snapshot(run_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            return
        raise
    if run.status not in {
        RunStatus.QUEUED.value,
        RunStatus.RUNNING.value,
        RunStatus.SYNTHESIZING.value,
    }:
        return
    try:
        await _launch_resume(
            run_id,
            expected_status=run.status,
            expected_lifecycle_revision=lifecycle_revision,
        )
    except HTTPException:
        logger.warning("Could not auto-resume run %s", run_id)


async def resume_interrupted_runs(run_ids: list[str]) -> None:
    for run_id in run_ids:
        await _resume_interrupted_run(run_id)


__all__ = ["_queue_resume_workflow"]
