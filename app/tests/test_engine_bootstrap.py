from __future__ import annotations

import asyncio
from typing import Any

import pytest
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.safety import gate as safety
from co_scientist.domains.safety.types import SafetyDecision
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.engine_tasks import inputs as engine_tasks_inputs
from co_scientist.orchestration.engine_tasks import support as engine_tasks_support
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform import db
from co_scientist.platform.db import checkpoints, runs
from co_scientist.platform.db.models import RunStatus as StoreRunStatus

from tests._client import create_run as _create_run
from tests._client import make_client
from tests._engine_tasks_helpers import (
    _Generator,
    _install_runtime,
    _patch_generator,
    _task_state,
)
from tests._store_helpers import enqueue_task, pause_run, seed_run


class _PauseDuringPrepare:
    def __init__(self, run_id: str, state: dict[str, Any]) -> None:
        self.run_id = run_id
        self.state = state

    async def prepare_task_state(self, *_: Any, **__: Any) -> dict[str, Any]:
        pause_run(self.run_id)
        return self.state


def _start_bootstrap(
    monkeypatch: pytest.MonkeyPatch, db_path: str, goal: str = "Bootstrap race"
) -> tuple[Any, str, Any]:
    client = make_client()
    created = _create_run(client, goal)
    run_id = str(created.json()["id"])
    assert client.post(f"/api/runs/{run_id}/start", json={}).status_code == 200
    task = store.claim_task("bootstrap-worker", run_id=run_id, db_path=db_path)
    assert task is not None
    return client, run_id, task


def _stub_bootstrap_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_intake(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(engine_tasks_inputs, "_screen_bootstrap_intake", no_intake)
    monkeypatch.setattr(engine_tasks_inputs, "sync_engine_llm_backend", lambda *_: None)


def _pause_during_bootstrap_prepare(
    monkeypatch: pytest.MonkeyPatch, db_path: str
) -> tuple[Any, str, Any]:
    client, run_id, task = _start_bootstrap(monkeypatch, db_path, "Pause during bootstrap prepare")
    _stub_bootstrap_providers(monkeypatch)
    _patch_generator(monkeypatch, _PauseDuringPrepare(run_id, _task_state(run_id)))
    monkeypatch.setattr(
        engine_tasks_support,
        "_enqueue_node_portfolio",
        _enqueue_bootstrap_successor,
    )
    return client, run_id, task


def _enqueue_bootstrap_successor(
    task: Any,
    _state: dict[str, Any],
    _successor: str | None,
    successor_type: str,
    conn: Any,
) -> Any:
    return enqueue_task(
        task.run_id,
        successor_type,
        f"{successor_type}:after:{task.id}",
        inputs={"checkpoint_seq": 1},
        dependencies=(task.id,),
        conn=conn,
    )


def _replace_expired_bootstrap_lease(client: Any, run_id: str, original: Any, db_path: str) -> Any:
    with db.transaction(db_path) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET lease_expires_at=0 WHERE id=?",
            (original.id,),
        )
    assert store.claim_task("new-bootstrap", run_id=run_id) is None
    paused = store.get_task(original.id, db_path=db_path)
    assert paused is not None and paused.status == "paused"
    assert "may have accepted" in (paused.error or "")
    assert (
        client.post(
            f"/api/runs/{run_id}/resume", json={"acknowledge_unknown_outcome": True}
        ).status_code
        == 200
    )
    replacement = store.claim_task("old-bootstrap", run_id=run_id)
    assert replacement is not None
    assert replacement.attempt == original.attempt + 1
    return replacement


def _forbid_contextual_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch both wrapper and direct-import namespaces so accidental contextual
    # calls cannot evade the guard.

    async def _fail_if_called(*_: Any, **__: Any) -> Any:
        raise AssertionError("offline-backed run escalated to the contextual safety model")

    monkeypatch.setattr(safety, "screen_contextual", _fail_if_called)
    monkeypatch.setattr(engine_tasks, "screen_contextual", _fail_if_called, raising=False)


@pytest.mark.asyncio
async def test_bootstrap_commits_state_and_enqueues_supervisor(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("Task-level science")
    bootstrap = engine_tasks.enqueue_bootstrap(run.id, db_path=isolated_db)
    leased = store.claim_task("worker", run_id=run.id, db_path=isolated_db)
    assert leased is not None and leased.id == bootstrap.id
    generator = _Generator(_task_state(run.id))
    _patch_generator(monkeypatch, generator, screen=True)

    result = await engine_tasks.execute_bootstrap(leased, db_path=isolated_db)
    assert lifecycle.complete_task(leased.id, "worker", result, db_path=isolated_db)

    checkpoint = checkpoints.get_latest_checkpoint(run.id, db_path=isolated_db)
    assert checkpoint is not None and checkpoint["seq"] == 1
    tasks = store.list_tasks(run.id, db_path=isolated_db)
    assert [task.task_type for task in tasks] == [
        "engine.bootstrap",
        "engine.node.supervisor",
        "engine.node.generate",
    ]
    assert [task.dependencies for task in tasks] == [
        (),
        (bootstrap.id,),
        (tasks[1].id,),
    ]


@pytest.mark.asyncio
async def test_bootstrap_never_escalates_an_offline_backed_run(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Offline bootstrap must not ask a contextual model whose uncertain verdict
    # could pause deterministic runs.
    run = seed_run("Task-level science", llm_backend="offline", db_path=isolated_db)
    engine_tasks.enqueue_bootstrap(run.id, db_path=isolated_db)
    leased = store.claim_task("worker", run_id=run.id, db_path=isolated_db)
    assert leased is not None
    _patch_generator(monkeypatch, _Generator(_task_state(run.id)))
    _forbid_contextual_escalation(monkeypatch)

    result = await engine_tasks.execute_bootstrap(leased, db_path=isolated_db)

    assert result.get("status") != "withheld"
    assert "checkpoint_seq" in result
    refreshed = runs.get_run(run.id, db_path=isolated_db)
    assert refreshed is not None and refreshed.status != "paused"


def _cancel_after_second_run_read(run_id: str, client: Any) -> Any:
    real_get_run = runs.get_run
    run_reads = 0

    def cancel_after_read(
        requested_run_id: str,
        db_path: str | None = None,
        conn: Any | None = None,
    ) -> Any:
        nonlocal run_reads
        run = real_get_run(requested_run_id, db_path=db_path, conn=conn)
        if requested_run_id == run_id:
            run_reads += 1
            if run_reads == 2:
                response = client.post(f"/api/runs/{run_id}/cancel")
                assert response.status_code == 200
        return run

    return cancel_after_read


@pytest.mark.asyncio
async def test_cancel_after_bootstrap_read_cannot_be_overwritten(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, run_id, task = _start_bootstrap(monkeypatch, isolated_db)
    real_get_run = runs.get_run
    monkeypatch.setattr(runs, "get_run", _cancel_after_second_run_read(run_id, client))
    prepared: list[bool] = []

    async def prepare_without_providers(*_: Any, **__: Any) -> tuple[Any, ...]:
        prepared.append(True)
        return {}, object()

    async def emit_without_persisting(*_: Any, **__: Any) -> None:
        return None

    _stub_bootstrap_providers(monkeypatch)
    monkeypatch.setattr(
        engine_tasks_inputs,
        "_prepare_bootstrap_state",
        prepare_without_providers,
    )
    monkeypatch.setattr(
        engine_tasks_inputs,
        "_save_state_and_enqueue",
        lambda *_: (1, "successor"),
    )
    monkeypatch.setattr(
        engine_tasks_inputs,
        "make_emitter",
        lambda *_args, **_kwargs: emit_without_persisting,
    )

    result = await engine_tasks.execute_bootstrap(task, db_path=isolated_db)

    refreshed = real_get_run(run_id, db_path=isolated_db)
    assert refreshed is not None and refreshed.status == "cancelled"
    assert result.get("status") == "cancelled"
    assert prepared == []
    persisted = store.get_task(task.id, db_path=isolated_db)
    assert persisted is not None and persisted.status == "cancelled"


def _assert_no_intake_verdict_applied(run_id: str, db_path: str) -> list[Any]:
    assert [
        item
        for item in records.list_safety_decisions(run_id, db_path=db_path)
        if item["stage"] == "intake"
    ] == []
    events = store_events.list_events(run_id, db_path=db_path)
    assert not any(event["type"] == "safety.intake" for event in events)
    return events


async def _hold_intake_screen(
    monkeypatch: pytest.MonkeyPatch, task: Any, decision: str, db_path: str
) -> tuple[asyncio.Task[Any], asyncio.Event]:
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_screen(*_: Any, **__: Any) -> SafetyDecision:
        started.set()
        await release.wait()
        return SafetyDecision(stage="intake", decision=decision, reason=f"injected {decision}")

    _install_runtime(monkeypatch).screen = delayed_screen
    bootstrap = asyncio.create_task(engine_tasks.execute_bootstrap(task, db_path=db_path))
    await asyncio.wait_for(started.wait(), timeout=5)
    return bootstrap, release


@pytest.mark.parametrize("decision", ["block", "hold"])
@pytest.mark.asyncio
async def test_stale_bootstrap_lease_cannot_apply_intake_stop(
    manual_worker: None,
    decision: str,
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, run_id, original = _start_bootstrap(monkeypatch, isolated_db)
    bootstrap, release = await _hold_intake_screen(monkeypatch, original, decision, isolated_db)
    _replace_expired_bootstrap_lease(client, run_id, original, isolated_db)
    release.set()

    with pytest.raises(task_worker.LeaseLostError):
        await bootstrap

    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == "queued"
    events = _assert_no_intake_verdict_applied(run_id, isolated_db)
    assert not any(
        event["type"] == "status"
        and event["payload"].get("status") in {"blocked", "paused"}
        and event["payload"].get("failure_kind") != "llm_interrupted_unknown"
        for event in events
    )


@pytest.mark.asyncio
async def test_replaced_bootstrap_lease_cannot_prepare_paused_run(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, run_id, original = _start_bootstrap(monkeypatch, isolated_db)
    replacement = _replace_expired_bootstrap_lease(client, run_id, original, isolated_db)
    runs.update_run_status(run_id, StoreRunStatus.PAUSED, db_path=isolated_db)
    prepared: list[bool] = []

    async def fake_prepare(*_: Any, **__: Any) -> tuple[Any, ...]:
        prepared.append(True)
        return {}, object()

    _stub_bootstrap_providers(monkeypatch)
    monkeypatch.setattr(engine_tasks_inputs, "_prepare_bootstrap_state", fake_prepare)

    with pytest.raises(task_worker.LeaseLostError):
        await engine_tasks.execute_bootstrap(original, db_path=isolated_db)

    assert prepared == []
    assert (
        runs.mark_bootstrap_running(
            run_id,
            replacement.id,
            replacement.lease_owner,
            replacement.attempt,
            db_path=isolated_db,
        )
        == StoreRunStatus.PAUSED.value
    )


@pytest.mark.asyncio
async def test_bootstrap_pause_without_resume_commits_paused_checkpoint(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run_id, task = _pause_during_bootstrap_prepare(monkeypatch, isolated_db)

    result = await engine_tasks.execute_bootstrap(task, db_path=isolated_db)

    assert result["status"] == "paused"
    checkpoint = checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert checkpoint is not None
    assert checkpoint["stage"] == f"engine_task_paused:{task.id}"
    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == "paused"
    assert len(store.list_tasks(run_id, db_path=isolated_db)) == 1
