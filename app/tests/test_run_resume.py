from __future__ import annotations

import asyncio
from typing import Any

import pytest
from co_scientist.api.runs import lifecycle as runs_lifecycle
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.domains.research_state.repository import records as store
from co_scientist.domains.research_state.repository.hypotheses import NewHypothesis
from co_scientist.domains.research_state.repository.records import NewEvidence, NewReview
from co_scientist.orchestration import task_worker
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import runs_views as views
from co_scientist.orchestration.repository import tasks as store_tasks
from co_scientist.platform.db import checkpoints, runs
from co_scientist.platform.db.models import RunStatus as StoreRunStatus

from tests._client import create_run as _create_run
from tests._client import make_client as _client
from tests._store_helpers import (
    enqueue_task,
    event_seqs,
    pause_run,
    resume_run,
    seed_checkpoint,
    seed_run,
)
from tests._task_worker_helpers import drain_run_tasks


def _new_run(client: Any, headers: dict[str, str] | None = None) -> str:
    res = _create_run(client, "Scientist-in-the-loop goal", headers=headers)
    return str(res.json()["id"])


def _seed_agent_artifacts(run_id: str) -> str:
    agent_id = hypotheses.add_hypothesis(
        NewHypothesis(
            run_id=run_id,
            title="Agent idea",
            statement="An agent-generated hypothesis.",
            created_by_agent="generation",
        )
    )
    store.add_review(
        NewReview(
            run_id=run_id,
            hypothesis_id=agent_id,
            reviewer_agent="reflection",
            summary="agent review",
            critique="agent critique",
        )
    )
    store.add_evidence(
        NewEvidence(
            run_id=run_id,
            title="Retrieved paper",
            source="pubmed",
            abstract="x",
        )
    )
    return agent_id


def _seed_scientist_artifacts(run_id: str) -> str:
    manual_id = hypotheses.add_hypothesis(
        NewHypothesis(
            run_id=run_id,
            title="Human idea",
            statement="A scientist-authored hypothesis.",
            created_by_agent="scientist_manual",
            author="dr-who",
        )
    )
    store.add_review(
        NewReview(
            run_id=run_id,
            hypothesis_id=manual_id,
            reviewer_agent="scientist",
            summary="human review",
            critique="looks promising",
        )
    )
    store.add_evidence(
        NewEvidence(
            run_id=run_id,
            title="Attached doc",
            source="attachment",
            abstract="notes",
        )
    )
    return manual_id


def test_resuming_a_pre_engine_checkpoint_restarts_from_a_fresh_bootstrap(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client()
    run_id = _new_run(client)
    _seed_agent_artifacts(run_id)
    manual_id = _seed_scientist_artifacts(run_id)
    seed_checkpoint(
        run_id,
        {"provider": "mock", "legacy": True},
        stage="iteration_1",
        last_event_seq=store_events.latest_event_seq(run_id),
    )
    runs.update_run_status(run_id, StoreRunStatus.PAUSED)

    resume_run(run_id)

    assert [h["id"] for h in hypotheses.list_hypotheses(run_id)] == [manual_id]
    assert checkpoints.get_latest_checkpoint(run_id) is None
    assert event_seqs(run_id, "lifecycle", event="legacy_resume_cleanup")
    [task] = store_tasks.list_tasks(run_id)
    assert (task.task_type, task.status) == ("engine.bootstrap", "queued")


def test_resume_reassigns_event_seqs_above_last_checkpoint(
    isolated_db: str,
) -> None:
    # Clients retain after=N cursors; resumed event sequences must exceed
    # checkpoint high-water marks.
    run = seed_run("Seq continuity", profile="express")
    for i in range(5):
        store_events.append_event(run.id, "log", {"i": i})
    high_water = store_events.latest_event_seq(run.id)
    assert high_water == 5

    seed_checkpoint(run.id, {}, stage="pause", last_event_seq=high_water)
    views.clear_run_derived_data(run.id)
    assert store_events.list_events(run.id) == []

    seq = store_events.append_event(run.id, "status", {"status": "resuming"})
    assert seq == high_water + 1
    assert store_events.list_events(run.id, after_seq=high_water)[0]["seq"] == seq


_WORKER = "double-resume-test"


async def _advance(run_id: str, count: int, db_path: str) -> None:
    for _ in range(count):
        worked = await task_worker.run_once(_WORKER, run_id=run_id, db_path=db_path)
        assert worked, "run finished (or stalled) earlier than the test expects"


async def _advance_until_pool_nonempty(run_id: str, db_path: str, *, cap: int = 30) -> set[str]:
    # Wait for a populated checkpoint rather than a task count tied to
    # generation fan-out topology.
    for _ in range(cap):
        worked = await task_worker.run_once(_WORKER, run_id=run_id, db_path=db_path)
        assert worked, "run finished before its pool ever grew"
        pool = _checkpoint_hypothesis_ids(run_id, db_path)
        if pool:
            return pool
    raise AssertionError(f"pool still empty after {cap} tasks")


def _pause_and_resume(run_id: str) -> None:
    pause_run(run_id)
    resume_run(run_id)


def _checkpoint_hypothesis_ids(run_id: str, db_path: str) -> set[str]:
    checkpoint = checkpoints.get_latest_checkpoint(run_id, db_path=db_path)
    assert checkpoint is not None
    payload = checkpoint["state"]["state"]
    return {str(h["id"]) for h in payload.get("hypotheses") or []}


@pytest.mark.asyncio
async def test_two_resume_cycles_still_complete_with_pool_intact(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Disable the embedded worker so two interruption boundaries cannot race
    # detached execution.
    client = _client()
    created = _create_run(client, "Double resume coverage", tier="express")
    assert created.status_code == 200
    run_id = created.json()["id"]
    started = client.post(f"/api/runs/{run_id}/start", json={})
    assert started.status_code == 200

    await _advance(run_id, 1, isolated_db)
    _pause_and_resume(run_id)

    pool_before = await _advance_until_pool_nonempty(run_id, isolated_db)
    _pause_and_resume(run_id)

    await drain_run_tasks(run_id, _WORKER, db_path=isolated_db)

    final_run = runs.get_run(run_id, db_path=isolated_db)
    assert final_run is not None
    assert final_run.status == "completed", final_run.error
    report = reports.get_latest_report(run_id, db_path=isolated_db)
    assert report is not None
    final_ids = {str(row["id"]) for row in hypotheses.list_hypotheses(run_id, db_path=isolated_db)}
    assert pool_before <= final_ids


def _enqueue_paused_blocking_task(run_id: str, db_path: str) -> None:
    enqueue_task(run_id, "engine.test.blocking", "blocking:0", db_path=db_path)
    pause_run(run_id, db_path=db_path)


def _install_blocking_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    from co_scientist.orchestration import engine_tasks

    async def _execute(_task: Any, *, db_path: str | None = None) -> dict[str, bool]:
        _time.sleep(1.0)
        return {"completed": True}

    monkeypatch.setattr(engine_tasks, "execute_engine_task", _execute)


async def _worst_loop_stall(stop: asyncio.Event) -> float:
    import time as _time

    worst = 0.0
    while not stop.is_set():
        started = _time.monotonic()
        await asyncio.sleep(0.01)
        worst = max(worst, _time.monotonic() - started - 0.01)
    return worst


async def test_resume_does_not_execute_run_work_on_the_event_loop(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resume cohorts must run off the API loop; synchronous state/SQLite work
    # otherwise starves health checks.
    run = seed_run("loop freedom")
    _enqueue_paused_blocking_task(run.id, isolated_db)
    _install_blocking_execute(monkeypatch)

    stop = asyncio.Event()
    probe = asyncio.create_task(_worst_loop_stall(stop))
    await runs_lifecycle._launch_resume(run.id)
    await asyncio.gather(*list(runs_lifecycle._resume_tasks))
    stop.set()
    worst_stall = await probe

    assert worst_stall < 0.5, f"event loop stalled {worst_stall:.2f}s while a resumed run executed"
