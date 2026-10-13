"""Durable worker for queued Co-Scientist run tasks, run as a per-run cohort inside the API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from co_scientist.core import inflight
from co_scientist.core.config import settings
from co_scientist.orchestration import engine_tasks
from co_scientist.orchestration.notifications import deliver_completion_notification
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository import tasks_lifecycle as store
from co_scientist.orchestration.task_worker.enqueue import (
    enqueue_run_workflow as enqueue_run_workflow,
)
from co_scientist.orchestration.task_worker.outcomes import (
    LeaseLostError as LeaseLostError,
)
from co_scientist.orchestration.task_worker.outcomes import (
    UnsupportedTaskError as UnsupportedTaskError,
)
from co_scientist.orchestration.task_worker.outcomes import (
    _handle_task_failure as _handle_task_failure,
)
from co_scientist.orchestration.task_worker.outcomes import (
    _record_success as _record_success,
)
from co_scientist.orchestration.task_worker.outcomes import failure_outcome
from co_scientist.platform.db.models import ScientificTask
from co_scientist.platform.llm.scoped_loop import run_in_scoped_loop
from co_scientist.platform.telemetry.logging_setup import run_log_context
from co_scientist.platform.telemetry.tracing import current_span

logger = logging.getLogger(__name__)

_EMAIL_TASK = "notification.email"

# Pids repeat across containers on one volume; the boot tag keeps this
# process's leases apart from a previous container's at shutdown release and
# startup recovery.
_PROCESS_TAG = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"
_process_worker_ids: set[str] = set()
_process_worker_ids_lock = threading.Lock()


def process_tag() -> str:
    return _PROCESS_TAG


def process_worker_ids() -> frozenset[str]:
    with _process_worker_ids_lock:
        return frozenset(_process_worker_ids)


@dataclass(frozen=True)
class _HeartbeatSignals:
    stop: asyncio.Event
    lease_lost: asyncio.Event


@dataclass(frozen=True)
class WorkerPolicy:
    db_path: str | None = None
    poll_seconds: float = 0.05
    lease_seconds: float = 300.0


# Long rate-limit parks use bounded slower polling, preserving due-time
# responsiveness without constant store queries.
_PARKED_POLL_SECONDS = 15.0


async def _execute_task_payload(task: ScientificTask, *, db_path: str | None) -> dict[str, Any]:
    if task.task_type.startswith(engine_tasks.ENGINE_TASK_PREFIX):
        started = time.perf_counter()
        outcome = "completed"
        stage_logger = logging.getLogger("app.run_stage")
        with run_log_context(task.run_id):
            stage_logger.info(
                "stage_start task=%s attempt=%s",
                task.task_type,
                task.attempt,
                extra={"diagnostic_event": "stage_start"},
            )
            try:
                return await engine_tasks.execute_engine_task(task, db_path=db_path)
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception:
                outcome = "failed"
                raise
            finally:
                stage_logger.info(
                    "stage_end task=%s outcome=%s duration_seconds=%.3f",
                    task.task_type,
                    outcome,
                    time.perf_counter() - started,
                    extra={"diagnostic_event": "stage_end"},
                )
    if task.task_type == _EMAIL_TASK:
        return await deliver_completion_notification(task.inputs)
    raise UnsupportedTaskError(f"unsupported task type: {task.task_type}")


async def _execute_until_lease_lost(
    task: ScientificTask,
    lease_lost: asyncio.Event,
    *,
    db_path: str | None,
) -> dict[str, Any]:
    """Lease revocation cancels in-flight provider and retrieval work
    instead of spending compute until natural completion.
    """
    with inflight.task_scope(task.id):
        execution = asyncio.create_task(_execute_task_payload(task, db_path=db_path))
    ownership = asyncio.create_task(lease_lost.wait())
    try:
        done, _ = await asyncio.wait({execution, ownership}, return_when=asyncio.FIRST_COMPLETED)
        if execution in done:
            return await execution

        execution.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await execution
        raise LeaseLostError(f"task {task.id} no longer owns its lease")
    finally:
        if not execution.done():
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
        ownership.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ownership


async def _execute_and_record(
    task: ScientificTask,
    worker_id: str,
    lease_lost: asyncio.Event,
    db_path: str | None,
) -> None:
    attributes = {
        "co_scientist.run_id": task.run_id,
        "co_scientist.task.id": task.id,
        "co_scientist.task.type": task.task_type,
        "co_scientist.task.node": task.task_type.removeprefix(engine_tasks.NODE_TASK_PREFIX),
        "co_scientist.task.attempt": task.attempt,
    }
    # Execution runs in a child asyncio task, which inherits this span.
    with current_span("task.execute", attributes) as span:
        outcome = await _execute_and_settle(task, worker_id, lease_lost, db_path)
        span.set_attribute("co_scientist.task.outcome", outcome)


async def _execute_and_settle(
    task: ScientificTask,
    worker_id: str,
    lease_lost: asyncio.Event,
    db_path: str | None,
) -> str:
    try:
        # Execution tasks inherit this run context and propagate it to child
        # tasks.
        with run_log_context(task.run_id):
            result = await _execute_until_lease_lost(task, lease_lost, db_path=db_path)
    except LeaseLostError:
        # Revoked workers must not overwrite already recorded cancellation,
        # pause or competing ownership.
        logger.info("Task %s stopped after lease revocation", task.id)
        return "lease_lost"
    except Exception as exc:  # Worker boundary isolates one task failure.
        from co_scientist.core.byok_scope import scoped_byok
        from co_scientist.domains.access.credentials import get_run_credential

        try:
            credential = get_run_credential(task.run_id, db_path=db_path)
        except Exception:
            # Decryption failure still needs a durable task outcome; no
            # plaintext exists to redact in that case.
            credential = None
        with run_log_context(task.run_id), scoped_byok(credential):
            _handle_task_failure(task, worker_id, exc, db_path)
        return failure_outcome(task, exc)
    _record_success(task, worker_id, result, db_path)
    return "committed"


async def _run_claimed_task(
    task: ScientificTask,
    worker_id: str,
    *,
    db_path: str | None,
    lease_seconds: float,
) -> None:
    stop_heartbeat = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_lease(
            task,
            worker_id,
            _HeartbeatSignals(stop=stop_heartbeat, lease_lost=lease_lost),
            db_path=db_path,
            lease_seconds=lease_seconds,
        )
    )
    try:
        await _execute_and_record(task, worker_id, lease_lost, db_path)
    finally:
        stop_heartbeat.set()
        await heartbeat


async def run_once(
    worker_id: str,
    *,
    run_id: str | None = None,
    db_path: str | None = None,
    lease_seconds: float = 300.0,
) -> bool:
    with inflight.operation_scope() as admitted:
        if not admitted:
            return False
        task = tasks.claim_task(
            worker_id,
            lease_seconds=lease_seconds,
            run_id=run_id,
            db_path=db_path,
        )
        if task is None:
            return False
        if inflight.shutdown_started():
            # This claim may have committed after the shutdown release ran.
            store.release_owned_leases([worker_id], db_path=db_path)
            return False
        await _run_claimed_task(task, worker_id, db_path=db_path, lease_seconds=lease_seconds)
        return True


async def _cohort_worker_step(
    run_id: str,
    worker_id: str,
    policy: WorkerPolicy,
) -> bool:
    """Keep workers alive for sibling leases and future-due queued work;
    read-only existence probes avoid growing per-tick row decoding.
    """
    if inflight.shutting_down():
        return False
    db_path = policy.db_path
    claimable, active_lease, parked_until = store.cohort_poll(run_id, db_path=db_path)
    if claimable and await run_once(
        worker_id,
        run_id=run_id,
        db_path=db_path,
        lease_seconds=policy.lease_seconds,
    ):
        return True
    if claimable or active_lease:
        await asyncio.sleep(policy.poll_seconds)
        return True
    if parked_until is not None:
        # Future-due rate-limit parks keep the cohort alive; exiting would
        # repeatedly strand still-valid queued work.
        await asyncio.sleep(min(_PARKED_POLL_SECONDS, max(0.0, parked_until - time.time())))
        return True
    return False


def _abandon_dead_leases_at_exit(run_id: str, db_path: str | None) -> None:
    """Idle exit is the last opportunity to settle abandoned spent leases;
    cleanup failure must not replace the run outcome.
    """
    try:
        abandoned = store.abandon_dead_leases(run_id, db_path=db_path)
    except Exception:
        logger.exception("Abandoning dead leases failed for run %s", run_id)
        return
    if abandoned:
        logger.warning(
            "Run %s: failed %d task(s) whose lease outlived its worker with no retries left.",
            run_id,
            abandoned,
        )


async def run_run_worker_pool(
    run_id: str,
    worker_prefix: str,
    *,
    worker_count: int | None = None,
    policy: WorkerPolicy | None = None,
) -> None:
    policy = policy or WorkerPolicy()
    if worker_count is None:
        worker_count = settings.worker_pool_size
    if worker_count < 1:
        raise ValueError("worker_count must be positive")

    async def _worker(index: int) -> None:
        worker_id = f"{worker_prefix}.{_PROCESS_TAG}:{index}"
        with _process_worker_ids_lock:
            _process_worker_ids.add(worker_id)
        while await _cohort_worker_step(run_id, worker_id, policy):
            continue

    await asyncio.gather(*(_worker(index) for index in range(worker_count)))
    with inflight.operation_scope() as admitted:
        if admitted:
            _abandon_dead_leases_at_exit(run_id, policy.db_path)


def run_run_worker_pool_sync(run_id: str, worker_prefix: str) -> None:
    """Embedded cohort serialization and writes run on a worker thread
    rather than the API event loop.
    """
    run_in_scoped_loop(run_run_worker_pool(run_id, worker_prefix))


async def _heartbeat_lease(
    task: ScientificTask,
    worker_id: str,
    signals: _HeartbeatSignals,
    *,
    db_path: str | None,
    lease_seconds: float,
) -> None:
    # Check cancellation promptly without writing each tick; renew the database
    # lease only on its own cadence.
    renew_every = max(0.05, lease_seconds / 3)
    interval = min(1.0, renew_every)
    due = time.monotonic() + renew_every
    while True:
        try:
            await asyncio.wait_for(signals.stop.wait(), timeout=interval)
            return
        except TimeoutError:
            if not store.owns_task_lease(task.id, worker_id, db_path=db_path):
                signals.lease_lost.set()
                return
            if time.monotonic() < due:
                continue
            due = time.monotonic() + renew_every
            if not store.renew_task_lease(
                task.id,
                worker_id,
                lease_seconds,
                db_path=db_path,
            ):
                logger.warning("Task %s lease heartbeat lost ownership", task.id)
                signals.lease_lost.set()
                return
