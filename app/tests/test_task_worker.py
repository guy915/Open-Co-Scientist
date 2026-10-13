from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from co_scientist.core.exceptions import (
    LLMCallBudgetExceededError,
    LLMRateLimitParkError,
)
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.orchestration.task_worker import outcomes as task_worker_outcomes
from co_scientist.platform import db as _store_db
from co_scientist.platform import db as store_db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus, ScientificTask
from co_scientist.platform.llm import current_run_call_count, scoped_llm_call_budget
from co_scientist.platform.llm.admission.call_budget import record_provider_request

from tests._engine_tasks_helpers import _enqueue, make_cancellable_executor
from tests._store_helpers import enqueue_task, seed_run


def test_enqueue_workflow_is_idempotent(isolated_db: str) -> None:
    run = seed_run("worker goal")
    first = task_worker.enqueue_run_workflow(run.id, db_path=isolated_db)
    duplicate = task_worker.enqueue_run_workflow(run.id, db_path=isolated_db)
    assert duplicate.id == first.id
    assert duplicate.task_type == "engine.bootstrap"


@pytest.mark.asyncio
async def test_worker_executes_and_commits_once(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("worker goal")
    task = enqueue_task(run.id, "engine.test.commit", "commit-once", db_path=isolated_db)

    async def _execute(_task: ScientificTask, *, db_path: str | None = None) -> dict[str, str]:
        runs.update_run_status(run.id, RunStatus.COMPLETED)
        return {"run_id": run.id, "status": "completed"}

    monkeypatch.setattr(engine_tasks, "execute_engine_task", _execute)
    assert await task_worker.run_once("worker-a", db_path=isolated_db)
    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.result == {"run_id": run.id, "status": "completed"}
    assert not await task_worker.run_once("worker-b", db_path=isolated_db)


@pytest.mark.asyncio
async def test_worker_completes_superseded_engine_task(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("superseded goal")
    task = enqueue_task(
        run.id,
        "engine.test.superseded",
        "superseded",
        max_attempts=3,
        db_path=isolated_db,
    )

    async def _execute(_task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        raise engine_tasks.SupersededTaskError("checkpoint advanced")

    monkeypatch.setattr(engine_tasks, "execute_engine_task", _execute)
    assert await task_worker.run_once("worker-a", db_path=isolated_db)
    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.attempt == 1
    assert saved.error is None
    assert saved.result == {
        "superseded": True,
        "reason": "checkpoint advanced",
    }


@pytest.mark.asyncio
async def test_worker_shutdown_cancels_task_payload(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("worker shutdown")
    enqueue_task(run.id, "engine.test.shutdown", "shutdown", db_path=isolated_db)
    started = asyncio.Event()
    interrupted = asyncio.Event()
    monkeypatch.setattr(
        engine_tasks,
        "execute_engine_task",
        make_cancellable_executor(started, interrupted),
    )
    running = asyncio.create_task(task_worker.run_once("worker-a", db_path=isolated_db))
    await asyncio.wait_for(started.wait(), timeout=1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert interrupted.is_set()


# An exhausted lease without a live owner cannot settle itself; recovery must
# make the run terminal.


_TASK_TYPE = f"{engine_tasks.NODE_TASK_PREFIX}ranking"


def _run_with_lease(
    db: str,
    *,
    expires_at: float,
    spend_budget: bool,
    goal: str = "stranded goal",
) -> tuple[str, str]:
    run = seed_run(goal)
    task = enqueue_task(
        run.id,
        _TASK_TYPE,
        f"{_TASK_TYPE}:1",
        inputs={"checkpoint_seq": 1},
        db_path=db,
    )
    extra = ", attempt=max_attempts" if spend_budget else ""
    with _store_db.connect(db) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET status='leased', lease_owner=?, "
            f"lease_expires_at=?{extra} WHERE id=?",
            ("dead", expires_at, task.id),
        )
        conn.execute("UPDATE runs SET status='running' WHERE id=?", (run.id,))
    return run.id, task.id


def _task_status(task_id: str, db: str) -> str:
    with _store_db.connect(db) as conn:
        row = conn.execute("SELECT status FROM scientific_tasks WHERE id=?", (task_id,)).fetchone()
    return str(row["status"])


@pytest.mark.parametrize(
    ("expires_in", "spend_budget", "abandoned", "task_status", "run_status"),
    [
        pytest.param(-3600, True, 1, "failed", "failed", id="dead-and-spent"),
        pytest.param(3600, True, 0, "leased", "running", id="live"),
        pytest.param(-3600, False, 0, "leased", "running", id="retries-left"),
    ],
)
def test_only_a_dead_spent_lease_is_abandoned_and_settles_its_run(
    isolated_db: str,
    expires_in: float,
    spend_budget: bool,
    abandoned: int,
    task_status: str,
    run_status: str,
) -> None:
    run_id, task_id = _run_with_lease(
        isolated_db,
        expires_at=time.time() + expires_in,
        spend_budget=spend_budget,
    )

    count = lifecycle.abandon_dead_leases(run_id, db_path=isolated_db)

    assert count == abandoned
    assert _task_status(task_id, isolated_db) == task_status
    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == run_status
    announced = [
        event
        for event in store_events.list_events(run_id, db_path=isolated_db)
        if event["type"] == "status" and event["payload"].get("status") == "failed"
    ]
    assert bool(announced) is bool(abandoned), "a settled run must announce it"


@pytest.mark.parametrize(("expires_in", "active"), [(-3600, False), (3600, True)])
def test_cohort_poll_reports_a_dead_spent_lease_as_inactive(
    isolated_db: str, expires_in: float, active: bool
) -> None:
    run_id, _ = _run_with_lease(isolated_db, expires_at=time.time() + expires_in, spend_budget=True)

    claimable, working, _ = lifecycle.cohort_poll(run_id, db_path=isolated_db)

    assert not claimable, "a spent-budget lease is claimable by nobody"
    assert working is active


@pytest.mark.asyncio
async def test_cohort_idle_exit_settles_a_run_left_with_a_dead_lease(
    isolated_db: str,
) -> None:
    run_id, task_id = _run_with_lease(isolated_db, expires_at=time.time() - 3600, spend_budget=True)

    await task_worker.run_run_worker_pool(
        run_id,
        "cohort",
        worker_count=1,
        policy=task_worker.WorkerPolicy(db_path=isolated_db),
    )

    assert _task_status(task_id, isolated_db) == "failed"
    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None
    assert run.status == "failed"


def _enqueue_test_tasks(run_id: str, count: int, prefix: str, db_path: str) -> None:
    for index in range(count):
        enqueue_task(run_id, f"engine.test.{index}", f"{prefix}:{index}", db_path=db_path)


def _assert_all_completed(run_id: str, db_path: str) -> None:
    assert {task.status for task in tasks.list_tasks(run_id, db_path=db_path)} == {"completed"}


def _count_lease_renewals(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    box = {"renewals": 0}

    def _renew(*_args: Any, **_kwargs: Any) -> bool:
        box["renewals"] += 1
        return True

    monkeypatch.setattr(lifecycle, "renew_task_lease", _renew)
    return box


class _ConcurrencyProbe:
    def __init__(self, sleep_seconds: float) -> None:
        self._sleep = sleep_seconds
        self._active = 0
        self.max_active = 0
        self._lock = asyncio.Lock()

    async def execute(
        self, _task: ScientificTask, *, db_path: str | None = None
    ) -> dict[str, bool]:
        async with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        await asyncio.sleep(self._sleep)
        async with self._lock:
            self._active -= 1
        return {"completed": True}


@pytest.mark.asyncio
async def test_worker_heartbeats_long_workflow_lease(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("long worker goal")
    task = enqueue_task(run.id, "engine.test.long", "long-engine-task", db_path=isolated_db)
    release = asyncio.Event()
    renewed = asyncio.Event()
    initial_time = time.time()
    clock = {"now": initial_time}
    monkeypatch.setattr(tasks, "current_time", lambda: clock["now"])
    monkeypatch.setattr(lifecycle, "current_time", lambda: clock["now"])
    real_renew = lifecycle.renew_task_lease

    def _renew(
        task_id: str, worker_id: str, lease_seconds: float, *, db_path: str | None = None
    ) -> bool:
        changed = real_renew(task_id, worker_id, lease_seconds, db_path=db_path)
        if changed:
            renewed.set()
        return changed

    async def _execute(_task: ScientificTask, *, db_path: str | None = None) -> dict[str, bool]:
        clock["now"] = initial_time + 0.1
        await release.wait()
        runs.update_run_status(run.id, RunStatus.COMPLETED)
        return {"completed": True}

    monkeypatch.setattr(lifecycle, "renew_task_lease", _renew)
    monkeypatch.setattr(engine_tasks, "execute_engine_task", _execute)
    running = asyncio.create_task(
        task_worker.run_once("worker-a", db_path=isolated_db, lease_seconds=0.15)
    )
    try:
        await asyncio.wait_for(renewed.wait(), timeout=5)
        saved = tasks.get_task(task.id, db_path=isolated_db)
        assert saved is not None
        assert saved.lease_owner == "worker-a"
        assert saved.lease_expires_at == pytest.approx(initial_time + 0.25, rel=0, abs=1e-6)
        # Beyond the initial deadline, but inside the actual renewed lease.
        clock["now"] = initial_time + 0.2
        assert not running.done()
        assert tasks.claim_task("worker-b", db_path=isolated_db) is None
    finally:
        release.set()
        assert await asyncio.wait_for(running, timeout=5)


@pytest.mark.asyncio
async def test_worker_cancels_execution_after_lease_revocation(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("cancel active work")
    task = enqueue_task(run.id, "engine.test.cancellable", "cancellable", db_path=isolated_db)
    started = asyncio.Event()
    interrupted = asyncio.Event()
    monkeypatch.setattr(
        engine_tasks,
        "execute_engine_task",
        make_cancellable_executor(started, interrupted),
    )
    running = asyncio.create_task(
        task_worker.run_once("worker-a", db_path=isolated_db, lease_seconds=0.15)
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert lifecycle.cancel_run_tasks(run.id, db_path=isolated_db) == 1
    assert await asyncio.wait_for(running, timeout=1)
    assert interrupted.is_set()
    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None
    assert saved.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_count", "task_count", "sleep", "lease_seconds", "min_overlap"),
    [
        pytest.param(4, 4, 0.03, 1, 4, id="explicit-pool"),
        # The default cohort must be wider than the old four-lease cap.
        pytest.param(None, 12, 0.05, 5, 5, id="default-pool"),
    ],
)
async def test_worker_cohort_executes_fanout_concurrently(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    worker_count: int | None,
    task_count: int,
    sleep: float,
    lease_seconds: float,
    min_overlap: int,
) -> None:
    run = seed_run("parallel goal")
    _enqueue_test_tasks(run.id, task_count, "parallel", isolated_db)
    probe = _ConcurrencyProbe(sleep)
    monkeypatch.setattr(engine_tasks, "execute_engine_task", probe.execute)
    policy = task_worker.WorkerPolicy(db_path=isolated_db, lease_seconds=lease_seconds)
    kwargs = {} if worker_count is None else {"worker_count": worker_count}

    await task_worker.run_run_worker_pool(run.id, "embedded-test", policy=policy, **kwargs)

    assert probe.max_active >= min_overlap
    _assert_all_completed(run.id, isolated_db)


@pytest.mark.asyncio
async def test_transient_provider_failure_keeps_its_retry_budget(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty provider responses are transient; classifying every ValueError as
    # permanent strands runs.
    run = seed_run("Transient failure")
    task = enqueue_task(run.id, "engine.node.ranking", "transient-1", db_path=isolated_db)

    async def empty_response(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ValueError("LLM returned None or empty content. Model: x")

    monkeypatch.setattr(task_worker, "_execute_task_payload", empty_response)
    assert await task_worker.run_once("w1", run_id=run.id, db_path=isolated_db)

    after = tasks.get_task(task.id, db_path=isolated_db)
    assert after is not None
    assert after.status == "queued", "a transient failure must stay retryable"
    assert after.attempt < after.max_attempts


@pytest.mark.asyncio
async def test_heartbeat_writes_on_the_lease_schedule_not_the_poll_schedule(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cancellation polls are memory-only; writing lease renewal every tick
    # starves the single SQLite writer.
    run = seed_run("heartbeat cost")
    task = _enqueue(run.id, "engine.test.heartbeat", "heartbeat:0", isolated_db)
    renewals = _count_lease_renewals(monkeypatch)

    stop = asyncio.Event()
    lease_lost = asyncio.Event()
    beat = asyncio.create_task(
        task_worker._heartbeat_lease(
            task,
            "worker-hb",
            task_worker._HeartbeatSignals(stop=stop, lease_lost=lease_lost),
            db_path=isolated_db,
            lease_seconds=300.0,
        )
    )
    await asyncio.sleep(2.5)
    stop.set()
    await beat

    count = renewals["renewals"]
    assert count == 0, f"{count} lease writes in 2.5s of polling"


def test_idle_claim_does_not_contend_for_the_write_lock(
    isolated_db: str,
) -> None:
    # Idle queue polls must not open write transactions or they starve unrelated
    # API writes.
    import sqlite3
    import time as _time

    assert tasks.claim_task("warmup", db_path=isolated_db) is None

    blocker = sqlite3.connect(isolated_db, timeout=0.5, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = _time.monotonic()
        assert tasks.claim_task("idle-worker", db_path=isolated_db) is None
        assert _time.monotonic() - started < 1.0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


# Call-budget exhaustion is permanent; retrying it cannot create more allowance.


def test_ceiling_exceeded_fails_permanently_and_settles_the_run(
    isolated_db: str,
) -> None:
    run = seed_run("LLM budget ceiling")
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    task = enqueue_task(
        run.id,
        "engine.node.generate",
        "generate:seed",
        max_attempts=5,
        db_path=isolated_db,
    )
    leased = tasks.claim_task("worker", run_id=run.id, db_path=isolated_db)
    assert leased is not None and leased.id == task.id
    with scoped_llm_call_budget(run.id, ceiling=1):
        record_provider_request()
    assert current_run_call_count(run.id) == 1

    task_worker_outcomes._handle_task_failure(
        leased,
        "worker",
        LLMCallBudgetExceededError(count=2501, ceiling=2500),
        isolated_db,
    )

    task_after = tasks.get_task(task.id, db_path=isolated_db)
    assert task_after is not None
    assert task_after.status == "failed", "a ceiling breach must not requeue"
    assert task_after.attempt < task_after.max_attempts, (
        "it must not have burned through the retry budget to get there"
    )
    settled = runs.get_run(run.id, db_path=isolated_db)
    assert settled is not None
    assert settled.status == RunStatus.FAILED.value
    assert settled.error is not None
    assert "LLM-call ceiling exceeded" in settled.error
    assert "2501" in settled.error and "2500" in settled.error
    assert current_run_call_count(run.id) == 0, (
        "a permanently failed run's counter must be dropped, not left to"
        " grow the process-wide tracker until the eviction cap"
    )


# Platform rate caps park without spending attempts or settling the run.


def _advance_clock(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    real_now = store_db.current_time()
    monkeypatch.setattr("co_scientist.platform.db.time.time", lambda: real_now + seconds)


def test_rate_limit_park_requeues_without_spending_an_attempt(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("Rate limit park")
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    task = enqueue_task(
        run.id,
        "engine.node.generate",
        "generate:seed",
        max_attempts=3,
        db_path=isolated_db,
    )
    leased = tasks.claim_task("worker", run_id=run.id, db_path=isolated_db)
    assert leased is not None and leased.id == task.id
    assert leased.attempt == 1
    resume_at = store_db.current_time() + 3600
    error = LLMRateLimitParkError(resume_at=resume_at, reason="message_per_day")

    task_worker_outcomes._handle_task_failure(leased, "worker", error, isolated_db)

    parked = tasks.get_task(task.id, db_path=isolated_db)
    assert parked is not None
    assert parked.status == "queued", "a platform cap must not fail the task"
    assert parked.attempt == 0, "parking must not consume an attempt"
    assert parked.lease_owner is None
    assert parked.available_at is not None
    assert resume_at <= parked.available_at <= resume_at + 15
    assert not tasks.claim_task("worker2", run_id=run.id, db_path=isolated_db)
    still_running = runs.get_run(run.id, db_path=isolated_db)
    assert still_running is not None
    assert still_running.status == RunStatus.RUNNING.value
    assert parked.attempts, "the park must be visible in attempt history"
    assert parked.attempts[-1]["retryable"] is True
    assert "message_per_day" in parked.attempts[-1]["error"]

    _advance_clock(monkeypatch, 3600 + 16)

    reclaimed = tasks.claim_task("worker2", run_id=run.id, db_path=isolated_db)
    assert reclaimed is not None and reclaimed.id == task.id


@pytest.mark.asyncio
async def test_cohort_keeps_polling_over_a_parked_task_instead_of_exiting(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Future-due parked rows keep the cohort alive even though they are neither
    # claimable nor leased.
    run = seed_run("Rate limit park cohort")
    task = enqueue_task(run.id, "engine.node.generate", "generate:seed", db_path=isolated_db)
    leased = tasks.claim_task("worker", run_id=run.id, db_path=isolated_db)
    assert leased is not None
    resume_at = store_db.current_time() + 3600
    ok = lifecycle.park_task_for_rate_limit(
        task.id, "worker", "rate limited", resume_at, db_path=isolated_db
    )
    assert ok

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("co_scientist.orchestration.task_worker.asyncio.sleep", _fake_sleep)

    policy = task_worker.WorkerPolicy(db_path=isolated_db)
    keep_going = await task_worker._cohort_worker_step(run.id, "worker2", policy)

    assert keep_going, "a pending park must keep the cohort alive"
    assert slept, "the idle loop must wait rather than busy-poll"
    assert slept[0] <= task_worker._PARKED_POLL_SECONDS
