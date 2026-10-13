from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest
from co_scientist.orchestration import engine_tasks
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform import db as store_db
from co_scientist.platform.db import checkpoints, runs
from co_scientist.platform.db.models import RunStatus, ScientificTask
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests._client import create_run as _create_run
from tests._client import make_client
from tests._store_helpers import (
    enqueue_task,
    event_seqs,
    pause_run,
    resume_run,
    seed_checkpoint,
)


def _checkpointed_run(db: str, client: Any) -> tuple[str, str]:
    response = _create_run(client, "Resume/cancel ordering", tier="express")
    assert response.status_code == 200, response.text
    run_id = str(response.json()["id"])
    runs.update_run_status(run_id, RunStatus.RUNNING, db_path=db)

    writer_type = f"{engine_tasks.NODE_TASK_PREFIX}generate"
    writer = enqueue_task(
        run_id,
        writer_type,
        "resume-cancel:writer",
        inputs={"checkpoint_seq": 0},
        db_path=db,
    )
    claimed = store.claim_task("checkpoint-writer", run_id=run_id, db_path=db)
    assert claimed is not None and claimed.id == writer.id
    assert lifecycle.complete_task(writer.id, "checkpoint-writer", {}, db_path=db)

    successor_type = f"{engine_tasks.NODE_TASK_PREFIX}ranking"
    checkpoint_seq = seed_checkpoint(
        run_id,
        {"provider": "engine", "resume_successor": successor_type},
        stage=f"engine_task:{writer.id}",
        db_path=db,
    )
    successor = enqueue_task(
        run_id,
        successor_type,
        f"{successor_type}:after:{writer.id}",
        inputs={"checkpoint_seq": checkpoint_seq},
        dependencies=(writer.id,),
        provenance={"scheduled_by": writer_type},
        db_path=db,
    )
    return run_id, successor.id


def _hold_resume_admission(
    monkeypatch: pytest.MonkeyPatch, *, first_only: bool = False
) -> tuple[Event, Event]:
    from co_scientist.api.runs import lifecycle as runs_lifecycle

    reached = Event()
    release = Event()
    original = runs_lifecycle._queue_resume_workflow

    def hold(*args: Any, **kwargs: Any) -> ScientificTask:
        if not (first_only and reached.is_set()):
            reached.set()
            assert release.wait(timeout=5), "resume barrier was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(runs_lifecycle, "_queue_resume_workflow", hold)
    return reached, release


def _assert_settled(run_id: str, successor_id: str, db: str, status: RunStatus) -> ScientificTask:
    run = runs.get_run(run_id, db_path=db)
    task = store.get_task(successor_id, db_path=db)
    assert run is not None and run.status == status.value
    assert task is not None
    assert store.claim_task("after-race", run_id=run_id, db_path=db) is None
    return task


def test_cancel_wins_when_it_commits_before_resume_enqueue(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = make_client()
    run_id, successor_id = _checkpointed_run(isolated_db, owner)
    pause_run(run_id, db_path=isolated_db)

    queue_reached, release_queue = _hold_resume_admission(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        resume_future = pool.submit(resume_run, run_id)
        assert queue_reached.wait(timeout=5), "resume did not reach enqueue boundary"
        cancelled = owner.post(f"/api/runs/{run_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        release_queue.set()
        with pytest.raises(HTTPException) as refused:
            resume_future.result(timeout=5)

    assert refused.value.status_code == 409
    task = _assert_settled(run_id, successor_id, isolated_db, RunStatus.CANCELLED)
    assert task.status == "cancelled"
    [cancelled_seq] = event_seqs(run_id, "status", status="cancelled", db_path=isolated_db)
    assert not [
        seq
        for seq in event_seqs(run_id, "status", status="resuming", db_path=isolated_db)
        if seq > cancelled_seq
    ]


def test_startup_resume_skips_cancelled_run_after_admission_race(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = make_client()
    run_id, successor_id = _checkpointed_run(isolated_db, owner)

    from co_scientist.api.runs import lifecycle as runs_lifecycle

    queue_reached, release_queue = _hold_resume_admission(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        startup = pool.submit(asyncio.run, runs_lifecycle.resume_interrupted_runs([run_id]))
        assert queue_reached.wait(timeout=5)
        assert owner.post(f"/api/runs/{run_id}/cancel").status_code == 200
        release_queue.set()
        startup.result(timeout=5)

    task = _assert_settled(run_id, successor_id, isolated_db, RunStatus.CANCELLED)
    assert task.status == "cancelled"
    [cancelled_seq] = event_seqs(run_id, "status", status="cancelled", db_path=isolated_db)
    assert not [
        seq
        for seq in event_seqs(run_id, "status", status="resuming", db_path=isolated_db)
        if seq > cancelled_seq
    ]


def _started_bootstrap(client: TestClient, goal: str, db: str) -> tuple[str, str]:
    run = _create_run(client, goal, tier="express")
    run_id = str(run.json()["id"])
    assert client.post(f"/api/runs/{run_id}/start", json={}).status_code == 200
    [bootstrap] = store.list_tasks(run_id, db_path=db)
    claimed = store.claim_task("bootstrap-owner", run_id=run_id, db_path=db)
    assert claimed is not None and claimed.id == bootstrap.id
    return run_id, bootstrap.id


@pytest.mark.parametrize("lease_state", ["live", "expired_retryable", "expired_spent"])
def test_resume_reuses_precheckpoint_bootstrap_lease(
    manual_worker: None,
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    lease_state: str,
) -> None:
    client = make_client()
    run_id, task_id = _started_bootstrap(client, f"Resume {lease_state} bootstrap", isolated_db)
    original = store.get_task(task_id, db_path=isolated_db)
    assert original is not None
    lease_expiry = time.time() + 3600
    if lease_state != "live":
        lease_expiry = time.time() - 3600
    with store_db.connect(isolated_db) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET lease_expires_at=?, "
            "attempt=CASE WHEN ? THEN max_attempts ELSE attempt END WHERE id=?",
            (lease_expiry, int(lease_state == "expired_spent"), task_id),
        )

    pause_run(run_id, db_path=isolated_db)
    paused_run = runs.get_run(run_id, db_path=isolated_db)
    assert paused_run is not None and paused_run.status == "paused"
    assert not checkpoints.has_checkpoint(run_id, db_path=isolated_db)

    resume_run(run_id)

    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == "queued"
    tasks = store.list_tasks(run_id, db_path=isolated_db)
    assert len(tasks) == 1 and tasks[0].id == task_id
    assert tasks[0].task_type == "engine.bootstrap"
    if lease_state == "live":
        assert tasks[0].status == "leased"
        assert tasks[0].lease_owner == "bootstrap-owner"
        assert store.claim_task("second-worker", run_id=run_id, db_path=isolated_db) is None
    elif lease_state == "expired_retryable":
        assert tasks[0].status == "queued"
        assert tasks[0].attempt == original.attempt
        reclaimed = store.claim_task("second-worker", run_id=run_id, db_path=isolated_db)
        assert reclaimed is not None and reclaimed.id == task_id
        assert reclaimed.attempt == original.attempt + 1
    else:
        assert tasks[0].status == "queued"
        assert tasks[0].attempt == original.max_attempts
        reclaimed = store.claim_task("second-worker", run_id=run_id, db_path=isolated_db)
        assert reclaimed is not None and reclaimed.id == task_id
        assert reclaimed.lease_owner == "second-worker"
        assert reclaimed.attempt == original.max_attempts + 1
    events = store_events.list_events(run_id, db_path=isolated_db)
    assert (
        sum(
            event["type"] == "status" and event["payload"].get("status") == "resuming"
            for event in events
        )
        == 1
    )
