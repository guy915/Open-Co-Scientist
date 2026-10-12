from __future__ import annotations

import asyncio

import pytest
from co_scientist.core import inflight
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.repository import tasks
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus, ScientificTask

from tests._store_helpers import enqueue_task, seed_run


def _leased(
    run_title: str,
    owner: str,
    db_path: str,
    idempotency: str,
    task_type: str = "engine.node.generate",
) -> ScientificTask:
    run = seed_run(run_title)
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=db_path)
    enqueue_task(run.id, task_type, idempotency, max_attempts=3, db_path=db_path)
    leased = tasks.claim_task(owner, run_id=run.id, db_path=db_path)
    assert leased is not None and leased.attempt == 1
    return leased


@pytest.mark.parametrize("task_type", ["engine.node.generate", "engine.fanout.review.item"])
def test_released_lease_is_claimable_at_once_without_spending_an_attempt(
    isolated_db: str, task_type: str
) -> None:
    leased = _leased(
        "release", "embedded-api:7.boot:0", isolated_db, f"{task_type}:release", task_type
    )

    assert tasks.release_owned_leases(["embedded-api:7.boot:0"], db_path=isolated_db) == 1

    released = tasks.get_task(leased.id, db_path=isolated_db)
    assert released is not None
    assert (released.status, released.attempt, released.lease_owner) == ("queued", 0, None)
    assert "graceful shutdown" in released.attempts[-1]["error"]
    reclaimed = tasks.claim_task("next-process:0", run_id=leased.run_id, db_path=isolated_db)
    assert reclaimed is not None and reclaimed.id == leased.id


def test_a_task_with_an_unanswered_call_keeps_its_lease(isolated_db: str) -> None:
    leased = _leased("in flight", "embedded-api:7.boot:0", isolated_db, "generate:inflight")

    released = tasks.release_owned_leases(
        ["embedded-api:7.boot:0"], keep_task_ids={leased.id}, db_path=isolated_db
    )

    assert released == 0
    kept = tasks.get_task(leased.id, db_path=isolated_db)
    assert kept is not None and kept.status == "leased"
    assert kept.lease_owner == "embedded-api:7.boot:0"


def test_another_process_lease_is_untouched(isolated_db: str) -> None:
    leased = _leased("foreign", "embedded-api:7.earlier:0", isolated_db, "generate:foreign")

    assert tasks.release_owned_leases(["embedded-api:7.boot:0"], db_path=isolated_db) == 0

    kept = tasks.get_task(leased.id, db_path=isolated_db)
    assert kept is not None and kept.lease_owner == "embedded-api:7.earlier:0"


def test_the_released_worker_can_no_longer_commit(isolated_db: str) -> None:
    leased = _leased("fenced", "embedded-api:7.boot:0", isolated_db, "generate:fenced")
    tasks.release_owned_leases(["embedded-api:7.boot:0"], db_path=isolated_db)

    assert not tasks.complete_task(
        leased.id, "embedded-api:7.boot:0", {"late": True}, db_path=isolated_db
    )


@pytest.mark.asyncio
async def test_unanswered_calls_survive_cancellation_and_answers_clear_them() -> None:
    async def call(answer: bool) -> None:
        mark = await inflight.begin_provider_call()
        if answer:
            mark.answered()
            return
        await asyncio.Event().wait()

    with inflight.task_scope("answered"):
        await call(True)
    with inflight.task_scope("cancelled"):
        pending = asyncio.create_task(call(False))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert inflight.begin_shutdown() == frozenset({"cancelled"})


@pytest.mark.asyncio
async def test_a_dispatch_after_shutdown_parks_instead_of_calling() -> None:
    inflight.begin_shutdown()
    with inflight.task_scope("late"):
        late = asyncio.create_task(inflight.begin_provider_call())
    await asyncio.sleep(0.05)

    assert not late.done()
    late.cancel()
    assert "late" not in inflight.begin_shutdown()


@pytest.mark.asyncio
async def test_shutdown_release_stops_a_running_worker_without_a_commit(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("running worker")
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    task = enqueue_task(run.id, "engine.node.generate", "generate:running", db_path=isolated_db)
    started = asyncio.Event()

    async def _execute(_task: ScientificTask, *, db_path: str | None = None) -> dict[str, str]:
        started.set()
        await asyncio.Event().wait()
        return {"status": "never"}

    monkeypatch.setattr(engine_tasks, "execute_engine_task", _execute)
    worker = asyncio.create_task(
        task_worker.run_once("embedded-api:7.boot:0", db_path=isolated_db, lease_seconds=3)
    )
    await asyncio.wait_for(started.wait(), timeout=5)

    tasks.release_owned_leases(
        ["embedded-api:7.boot:0"], keep_task_ids=inflight.begin_shutdown(), db_path=isolated_db
    )
    await asyncio.wait_for(worker, timeout=5)

    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None
    assert (saved.status, saved.attempt, saved.result) == ("queued", 0, None)
    assert not await task_worker.run_once("embedded-api:7.boot:1", db_path=isolated_db)


def test_lifespan_shutdown_releases_this_process_leases(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import co_scientist.main as main_module
    from fastapi.testclient import TestClient

    owner = "embedded-api:7.lifespan:0"
    monkeypatch.setattr(task_worker, "process_worker_ids", lambda: frozenset({owner}))
    with TestClient(main_module.app) as client:
        assert client.get("/health").status_code == 200
        leased = _leased("lifespan", owner, isolated_db, "generate:lifespan")

    released = tasks.get_task(leased.id, db_path=isolated_db)
    assert released is not None
    assert (released.status, released.attempt, released.lease_owner) == ("queued", 0, None)
