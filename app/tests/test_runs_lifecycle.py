from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import pairwise
from threading import Event, Timer
from typing import cast

import pytest
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus, ScientificTask
from fastapi.testclient import TestClient

from tests._client import DEFAULT_TEST_CLIENT_ID, make_client
from tests._client import create_run as _create_run
from tests._client import make_client as _client
from tests._engine_tasks_helpers import _seed_checkpoint, _task_state
from tests._store_helpers import enqueue_task, resume_run, seed_checkpoint, seed_run


def _new_run(c: TestClient, goal: str, *, tier: str = "express") -> str:
    response = _create_run(c, goal, tier=tier)
    return cast(str, response.json()["id"])


def test_cancel_after_capacity_reservation_prevents_bootstrap_admission(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_client = _client()
    cancel_client = _client()
    rid = _new_run(start_client, "Cancel between start admission steps")
    capacity_reserved = Event()
    cancel_entered_transaction = Event()
    continue_start = Event()
    reserve_capacity = runs.reserve_run_capacity_in_transaction
    cancel_tasks = lifecycle.cancel_run_tasks

    def reserve_then_wait(*args: object, **kwargs: object) -> bool:
        reserved = reserve_capacity(*args, **kwargs)  # type: ignore[arg-type]
        capacity_reserved.set()
        if not continue_start.wait(timeout=10):
            raise TimeoutError("test did not release the start barrier")
        return reserved

    def observe_cancel_tasks(*args: object, **kwargs: object) -> int:
        cancel_entered_transaction.set()
        return cancel_tasks(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runs, "reserve_run_capacity_in_transaction", reserve_then_wait)
    monkeypatch.setattr(lifecycle, "cancel_run_tasks", observe_cancel_tasks)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending_start = executor.submit(start_client.post, f"/api/runs/{rid}/start", json={})
        early_cancel = None
        try:
            assert capacity_reserved.wait(timeout=5)
            pending_cancel = executor.submit(cancel_client.post, f"/api/runs/{rid}/cancel")
            if cancel_entered_transaction.wait(timeout=0.5):
                early_cancel = pending_cancel.result(timeout=5)
        finally:
            continue_start.set()
        started = pending_start.result(timeout=5)
        cancelled = early_cancel if early_cancel is not None else pending_cancel.result(timeout=5)

    assert started.status_code == 200
    assert cancelled.status_code == 200
    assert start_client.get(f"/api/runs/{rid}").json()["status"] == "cancelled"
    [task] = store.list_tasks(rid, db_path=isolated_db)
    assert task.status == "cancelled"
    events = store_events.list_events(rid, db_path=isolated_db)
    cancelled_event = next(
        event
        for event in events
        if event["type"] == "status" and event["payload"].get("status") == "cancelled"
    )
    queued_event = next(
        event
        for event in events
        if event["type"] == "lifecycle" and event["payload"].get("event") == "queued"
    )
    assert queued_event["seq"] < cancelled_event["seq"]


@pytest.mark.parametrize("status", [RunStatus.CANCELLED, RunStatus.FAILED])
def test_checkpointed_run_is_resumed_not_restarted(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch, status: RunStatus
) -> None:
    client = _client()
    rid = _new_run(client, "Continue a checkpointed run")
    _seed_checkpoint(rid, _task_state(rid), db_path=isolated_db)
    runs.update_run_status(rid, status, db_path=isolated_db)

    restart = client.post(f"/api/runs/{rid}/start", json={})

    assert restart.status_code == 409
    assert "cannot be restarted" in restart.json()["detail"]
    assert store.list_tasks(rid, db_path=isolated_db) == []

    resume_run(rid)

    assert runs.get_run(rid, db_path=isolated_db).status == "queued"  # type: ignore[union-attr]
    [task] = store.list_tasks(rid, db_path=isolated_db)
    assert task.task_type == "engine.node.orchestrator"
    assert task.status == "queued"


def test_blocked_bootstrap_without_checkpoint_requires_new_run(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.db.models import RunStatus

    client = _client()
    rid = _new_run(client, "Do not revive an intake block")
    assert client.post(f"/api/runs/{rid}/start", json={}).status_code == 200
    task = store.claim_task("blocked-bootstrap", run_id=rid)
    assert task is not None
    assert lifecycle.complete_task(
        task.id,
        "blocked-bootstrap",
        {"status": "withheld"},
        db_path=isolated_db,
    )
    runs.update_run_status(rid, RunStatus.BLOCKED, db_path=isolated_db)

    restart = client.post(f"/api/runs/{rid}/start", json={})

    assert restart.status_code == 409
    assert "create a new run" in restart.json()["detail"]
    saved = store.get_task(task.id, db_path=isolated_db)
    assert saved is not None and saved.status == "completed"


def test_start_rolls_back_capacity_when_bootstrap_enqueue_fails(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    client = _client()
    rid = _new_run(client, "Rollback an incomplete start")

    def fail_enqueue(*_: object, **__: object) -> object:
        raise HTTPException(status_code=503, detail="injected enqueue failure")

    monkeypatch.setattr(engine_tasks, "enqueue_bootstrap", fail_enqueue)
    response = client.post(f"/api/runs/{rid}/start", json={})

    assert response.status_code == 503
    run = runs.get_run(rid, db_path=isolated_db)
    assert run is not None and run.status == "draft"
    assert store.list_tasks(rid, db_path=isolated_db) == []
    assert not any(
        event["type"] == "lifecycle" and event["payload"].get("event") == "queued"
        for event in store_events.list_events(rid, db_path=isolated_db)
    )


@pytest.mark.parametrize("terminal_event", [True, False])
def test_event_stream_tails_a_live_run_until_it_ends(
    isolated_db: str, terminal_event: bool
) -> None:
    run = seed_run(
        "Tail a live run",
        client_id=DEFAULT_TEST_CLIENT_ID,
        db_path=isolated_db,
    )
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    store_events.append_event(run.id, "log", {"i": 0}, db_path=isolated_db)

    def finish() -> None:
        # Without a terminal status event the stream must notice the run row.
        store_events.append_event(run.id, "status", {"status": "running"}, db_path=isolated_db)
        if terminal_event:
            store_events.append_event(
                run.id, "status", {"status": "completed"}, db_path=isolated_db
            )
        runs.update_run_status(run.id, RunStatus.COMPLETED, db_path=isolated_db)

    timer = Timer(0.3, finish)
    timer.start()
    body = make_client().get(f"/api/runs/{run.id}/events").text
    timer.join()

    frames = [json.loads(line[6:]) for line in body.splitlines() if line]
    assert [frame["type"] for frame in frames] == [
        "log",
        "status",
        *(["status"] if terminal_event else []),
        "_terminal",
    ]
    assert frames[-1]["payload"]["status"] == "completed"


_OLD_OWNER = "pre-restart-worker"


@dataclass
class _StartupProbe:
    recovery_finished: Event = field(default_factory=Event)
    worker_started: Event = field(default_factory=Event)
    task_scans: list[list[str]] = field(default_factory=list)
    cohort_run_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _PausedRestart:
    run_id: str
    writer_id: str
    successor_id: str
    paused_seq: int


def _seed_paused_restart(db_path: str) -> _PausedRestart:
    run = seed_run(
        "Pause restart acceptance",
        profile="express",
        client_id=DEFAULT_TEST_CLIENT_ID,
        db_path=db_path,
    )
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=db_path)
    writer = enqueue_task(
        run.id,
        f"{engine_tasks.NODE_TASK_PREFIX}generate",
        "pause-restart:writer",
        inputs={"checkpoint_seq": 0},
        db_path=db_path,
    )
    claimed = store.claim_task(_OLD_OWNER, run_id=run.id, lease_seconds=3600, db_path=db_path)
    assert claimed is not None and claimed.id == writer.id
    successor_id, paused_seq = _append_late_successor(run.id, writer, db_path)
    return _PausedRestart(run.id, writer.id, successor_id, paused_seq)


def _append_late_successor(run_id: str, writer: ScientificTask, db_path: str) -> tuple[str, int]:
    successor_type = f"{engine_tasks.NODE_TASK_PREFIX}ranking"
    runs.update_run_status(run_id, RunStatus.PAUSED, db_path=db_path)
    paused_seq = store_events.append_event(run_id, "status", {"status": "paused"}, db_path=db_path)
    checkpoint_seq = seed_checkpoint(
        run_id,
        {"provider": "engine", "resume_successor": successor_type},
        stage=f"engine_task_paused:{writer.id}",
        last_event_seq=paused_seq,
        db_path=db_path,
    )
    successor = enqueue_task(
        run_id,
        successor_type,
        f"{successor_type}:after:{writer.id}",
        inputs={"checkpoint_seq": checkpoint_seq},
        dependencies=(writer.id,),
        provenance={"scheduled_by": writer.task_type},
        db_path=db_path,
    )
    return successor.id, paused_seq


def _observe_startup(monkeypatch: pytest.MonkeyPatch) -> _StartupProbe:
    from co_scientist import main as main_module

    probe = _StartupProbe()
    original_start = main_module._start_recovery_task
    original_scan = store.list_active_engine_task_run_ids

    async def no_seed() -> None:
        return None

    def start_recovery(
        reconciled: dict[str, list[str]],
    ) -> tuple[asyncio.Task[None], list[asyncio.Task[None]]]:
        recovery, workers = original_start(reconciled)
        recovery.add_done_callback(lambda _: probe.recovery_finished.set())
        return recovery, workers

    def scan_active(db_path: str | None = None) -> list[str]:
        run_ids = original_scan(db_path)
        probe.task_scans.append(run_ids)
        return run_ids

    def record_cohort(run_id: str, _worker_id: str) -> None:
        probe.cohort_run_ids.append(run_id)
        probe.worker_started.set()

    monkeypatch.setattr(main_module, "seed_demo_runs", no_seed)
    monkeypatch.setattr(main_module, "_start_recovery_task", start_recovery)
    monkeypatch.setattr(store, "list_active_engine_task_run_ids", scan_active)
    monkeypatch.setattr(task_worker, "run_run_worker_pool_sync", record_cohort)
    return probe


def _assert_startup_left_run_paused(
    probe: _StartupProbe,
    state: _PausedRestart,
    db_path: str,
) -> None:
    assert probe.recovery_finished.wait(5), "startup recovery did not finish"
    assert probe.task_scans
    assert all(state.run_id not in run_ids for run_ids in probe.task_scans)
    assert probe.cohort_run_ids == []
    run = runs.get_run(state.run_id, db_path=db_path)
    writer = store.get_task(state.writer_id, db_path=db_path)
    successor = store.get_task(state.successor_id, db_path=db_path)
    assert run is not None and run.status == RunStatus.PAUSED.value
    assert writer is not None and writer.status == "leased"
    assert successor is not None and successor.status == "queued"
    assert store.claim_task("before-explicit-resume", run_id=state.run_id, db_path=db_path) is None


def _assert_resume_reuses_successor(
    client: TestClient,
    probe: _StartupProbe,
    state: _PausedRestart,
    db_path: str,
) -> None:
    before = store.list_tasks(state.run_id, db_path=db_path)
    before_ids = {task.id for task in before}
    assert before_ids == {state.writer_id, state.successor_id}
    resume_run(state.run_id)
    assert probe.worker_started.wait(5), "explicit resume did not launch"
    assert probe.cohort_run_ids == [state.run_id]
    after = store.list_tasks(state.run_id, db_path=db_path)
    assert {task.id for task in after} == before_ids
    successors = [
        task for task in after if task.task_type == f"{engine_tasks.NODE_TASK_PREFIX}ranking"
    ]
    assert len(successors) == 1 and successors[0].id == state.successor_id
    assert successors[0].dependencies == (state.writer_id,)
    assert lifecycle.complete_task(state.writer_id, _OLD_OWNER, {}, db_path=db_path)
    claimed = store.claim_task("after-explicit-resume", run_id=state.run_id, db_path=db_path)
    assert claimed is not None and claimed.id == state.successor_id
    _assert_ordered_event_replay(client, state.run_id, state.paused_seq)


def _assert_ordered_event_replay(client: TestClient, run_id: str, paused_seq: int) -> None:
    events = client.get(f"/api/runs/{run_id}/events?stream=false").json()["events"]
    seqs = [event["seq"] for event in events]
    assert all(left < right for left, right in pairwise(seqs))
    paused = next(event for event in events if event["seq"] == paused_seq)
    assert paused["type"] == "status"
    assert paused["payload"]["status"] == "paused"
    replay = client.get(f"/api/runs/{run_id}/events?stream=false&after={paused_seq}").json()[
        "events"
    ]
    assert replay == [event for event in events if event["seq"] > paused_seq]
    assert [event["payload"].get("status") for event in replay] == ["resuming"]


def test_restart_keeps_paused_successor_idle_until_explicit_resume(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    restart = _seed_paused_restart(isolated_db)
    probe = _observe_startup(monkeypatch)
    with make_client() as client:
        _assert_startup_left_run_paused(probe, restart, isolated_db)
        _assert_resume_reuses_successor(client, probe, restart, isolated_db)
