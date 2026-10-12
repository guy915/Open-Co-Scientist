from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from co_scientist.domains.report import build as report_build
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.domains.research_state.repository.hypotheses import NewHypothesis
from co_scientist.domains.safety.gate import SafetyDecision
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.engine_tasks import report_finalize
from co_scientist.orchestration.engine_tasks import support as engine_tasks_support
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import runs_views as views
from co_scientist.orchestration.repository import tasks as store
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus

from tests._client import create_run as _create_run
from tests._client import make_client
from tests._engine_tasks_helpers import (
    _Generator,
    _install_runtime,
    _patch_restore_generator,
    _seed_checkpoint,
    _task_state,
    fake_final_drain,
)
from tests._store_helpers import enqueue_task

_OWNER = {"X-Client-ID": "report-cancel-owner"}
_EMAIL = "scientist@example.org"


class _WorkerProcessCrashError(RuntimeError):
    pass


def _publication_event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    event_types = Counter(event["type"] for event in events)
    terminal_statuses = Counter(
        event["payload"].get("status") for event in events if event["type"] == "status"
    )
    return {
        "report": event_types["report"],
        "completed": terminal_statuses["completed"],
        "cancelled": terminal_statuses["cancelled"],
    }


def _seed_owned_finalize(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    claim: bool = True,
) -> tuple[Any, str, Any, str]:
    owner = make_client()
    created = _create_run(
        owner,
        "Study cancellation at report publication",
        headers=_OWNER,
        tier="express",
        notify_on_completion=True,
        completion_email=_EMAIL,
    )
    assert created.status_code == 200, created.text
    run_id = str(created.json()["id"])
    runs.update_run_status(run_id, RunStatus.RUNNING, db_path=isolated_db)
    hypothesis_id = hypotheses.add_hypothesis(
        NewHypothesis(
            run_id=run_id,
            title="IL-6 feedback",
            statement="IL-6 increases inflammation via STAT3 signaling.",
        ),
        db_path=isolated_db,
    )
    state = _task_state(run_id)
    checkpoint_seq = _seed_checkpoint(run_id, state, db_path=isolated_db)
    queued = enqueue_task(
        run_id,
        engine_tasks_support.FINALIZE_TASK,
        "cancel-finalize-publication",
        inputs={"checkpoint_seq": checkpoint_seq},
        db_path=isolated_db,
    )
    task = (
        store.claim_task("report-finalize-worker", run_id=run_id, db_path=isolated_db)
        if claim
        else store.get_task(queued.id, db_path=isolated_db)
    )
    assert task is not None and task.id == queued.id
    assert task.task_type == engine_tasks_support.FINALIZE_TASK
    assert task.status == ("leased" if claim else "queued")
    _patch_restore_generator(monkeypatch, _Generator(state))

    _install_runtime(monkeypatch).drain_final_state = fake_final_drain
    return owner, run_id, task, hypothesis_id


def _install_report_stubs(hypothesis_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    built = report_build._BuiltReport(
        payload={
            "research_goal": "Study cancellation at report publication",
            "leaderboard": [{"id": hypothesis_id, "title": "IL-6 feedback", "elo": 1500}],
        },
        markdown="# Goal Report\n\nIL-6 feedback.",
        facts=[
            {
                "hypothesis_id": hypothesis_id,
                "evidence_id": "fixture-evidence",
                "kind": "fact",
                "statement": "IL-6 increases inflammation.",
                "entities": ["IL6"],
                "state": "supports",
            }
        ],
        exclusion_tally={},
    )

    async def fake_build_report(*_: Any, **__: Any) -> Any:
        return built

    async def allow_final_screen(*_: Any, **__: Any) -> SafetyDecision:
        return SafetyDecision(stage="final", decision="allow")

    monkeypatch.setattr(report_finalize, "build_report_content", fake_build_report)
    _install_runtime(monkeypatch).screen = allow_final_screen


def _install_cancel_before_publication(
    owner: Any,
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    gate_readiness_and_publish = report_finalize._gate_readiness_and_publish
    cancel_responses: list[dict[str, Any]] = []

    async def cancel_before_publication(*args: Any, **kwargs: Any) -> Any:
        response = owner.post(f"/api/runs/{run_id}/cancel", headers=_OWNER)
        assert response.status_code == 200, response.text
        cancel_responses.append(response.json())
        async for event in gate_readiness_and_publish(*args, **kwargs):
            yield event

    monkeypatch.setattr(
        report_finalize,
        "_gate_readiness_and_publish",
        cancel_before_publication,
    )
    return cancel_responses


def _publication_snapshot(
    owner: Any,
    run_id: str,
    task_id: str,
    cancel_responses: list[dict[str, Any]],
    isolated_db: str,
) -> dict[str, Any]:
    reconciliation = views.reconcile_interrupted_runs(db_path=isolated_db)
    persisted_run = runs.get_run(run_id, db_path=isolated_db)
    task_after = store.get_task(task_id, db_path=isolated_db)
    assert persisted_run is not None and task_after is not None
    events = store_events.list_events(run_id, db_path=isolated_db)
    tasks = store.list_tasks(run_id, db_path=isolated_db)
    event_counts = _publication_event_counts(events)
    task_types = [item.task_type for item in tasks]
    final_run = runs.get_run(run_id, db_path=isolated_db)
    assert final_run is not None

    return {
        "cancel_reached_publication_boundary": cancel_responses
        == [{"id": run_id, "status": "cancelled"}],
        "run_status": persisted_run.status,
        "task_status": task_after.status,
        "report_row_exists": reports.get_latest_report(run_id, db_path=isolated_db) is not None,
        "owner_report_status": owner.get(f"/api/runs/{run_id}/report", headers=_OWNER).status_code,
        "owner_markdown_status": owner.get(
            f"/api/runs/{run_id}/report.md", headers=_OWNER
        ).status_code,
        "knowledge_fact_count": len(reports.list_knowledge_facts(run_id, db_path=isolated_db)),
        "report_event_count": event_counts["report"],
        "completed_event_count": event_counts["completed"],
        "cancelled_event_count": event_counts["cancelled"],
        "email_task_count": task_types.count("notification.email"),
        "restart_run_status": final_run.status,
        "restart_reconciles_as_active": run_id in reconciliation["failed"]
        or run_id in reconciliation["resumable"],
    }


@pytest.mark.asyncio
async def test_cancel_after_final_safety_withholds_report_publication(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, run_id, task, hypothesis_id = _seed_owned_finalize(isolated_db, monkeypatch)
    _install_report_stubs(hypothesis_id, monkeypatch)
    cancel_responses = _install_cancel_before_publication(owner, run_id, monkeypatch)

    with pytest.raises(task_worker.LeaseLostError):
        await engine_tasks.execute_finalize(task, db_path=isolated_db)

    actual = _publication_snapshot(owner, run_id, task.id, cancel_responses, isolated_db)
    assert actual == {
        "cancel_reached_publication_boundary": True,
        "run_status": RunStatus.CANCELLED.value,
        "task_status": "cancelled",
        "report_row_exists": False,
        "owner_report_status": 404,
        "owner_markdown_status": 404,
        "knowledge_fact_count": 0,
        "report_event_count": 0,
        "completed_event_count": 0,
        "cancelled_event_count": 1,
        "email_task_count": 0,
        "restart_run_status": RunStatus.CANCELLED.value,
        "restart_reconciles_as_active": False,
    }


@pytest.mark.asyncio
async def test_normal_finalize_publishes_and_survives_restart(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, run_id, _task, hypothesis_id = _seed_owned_finalize(
        isolated_db, monkeypatch, claim=False
    )
    _install_report_stubs(hypothesis_id, monkeypatch)

    assert await task_worker.run_once("normal-report-worker", run_id=run_id, db_path=isolated_db)

    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == RunStatus.COMPLETED.value
    report_response = owner.get(f"/api/runs/{run_id}/report", headers=_OWNER)
    markdown_response = owner.get(f"/api/runs/{run_id}/report.md", headers=_OWNER)
    assert report_response.status_code == 200
    assert report_response.json()["payload"]["research_goal"] == (
        "Study cancellation at report publication"
    )
    assert markdown_response.status_code == 200
    assert "IL-6 feedback" in markdown_response.text
    assert len(reports.list_knowledge_facts(run_id, db_path=isolated_db)) == 1

    events = store_events.list_events(run_id, db_path=isolated_db)
    counts = _publication_event_counts(events)
    report_event = next(event for event in events if event["type"] == "report")
    completion_event = next(
        event
        for event in events
        if event["type"] == "status" and event["payload"].get("status") == "completed"
    )
    assert counts == {"report": 1, "completed": 1, "cancelled": 0}
    assert report_event["seq"] < completion_event["seq"]

    email_tasks = [
        task
        for task in store.list_tasks(run_id, db_path=isolated_db)
        if task.task_type == "notification.email"
    ]
    assert not email_tasks

    reconciled = views.reconcile_interrupted_runs(db_path=isolated_db)
    assert run_id not in reconciled["failed"]
    assert run_id not in reconciled["resumable"]
    persisted_report = reports.get_latest_report(run_id, db_path=isolated_db)
    assert persisted_report is not None
    assert owner.get(f"/api/runs/{run_id}/report", headers=_OWNER).status_code == 200
    cancelled = owner.post(f"/api/runs/{run_id}/cancel", headers=_OWNER)
    assert cancelled.status_code == 409
    persisted = runs.get_run(run_id, db_path=isolated_db)
    assert persisted is not None
    assert persisted.status == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_restart_does_not_strand_finalize_lease_after_report_commit(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, run_id, _task, hypothesis_id = _seed_owned_finalize(
        isolated_db, monkeypatch, claim=False
    )
    _install_report_stubs(hypothesis_id, monkeypatch)

    def crash_before_ack(*_: Any, **__: Any) -> None:
        raise _WorkerProcessCrashError("process stopped before complete_task")

    monkeypatch.setattr(task_worker, "_record_success", crash_before_ack)
    with pytest.raises(_WorkerProcessCrashError, match="before complete_task"):
        await task_worker.run_once("crashed-report-worker", run_id=run_id, db_path=isolated_db)

    report = reports.get_latest_report(run_id, db_path=isolated_db)
    run = runs.get_run(run_id, db_path=isolated_db)
    task_rows = store.list_tasks(run_id, db_path=isolated_db)
    finalize = next(
        task for task in task_rows if task.task_type == engine_tasks_support.FINALIZE_TASK
    )
    assert report is not None
    assert run is not None and run.status == RunStatus.COMPLETED.value
    assert finalize.status == "leased"

    reconciliation = views.reconcile_interrupted_runs(db_path=isolated_db)
    recovered_runs = store.list_active_engine_task_run_ids(db_path=isolated_db)
    recovered_task = store.get_task(finalize.id, db_path=isolated_db)
    assert recovered_task is not None
    assert run_id not in reconciliation["failed"]
    assert run_id not in reconciliation["resumable"]
    assert run_id not in recovered_runs
    assert recovered_task.status == "completed"
    assert reports.get_latest_report(run_id, db_path=isolated_db) is not None
    assert owner.get(f"/api/runs/{run_id}/report", headers=_OWNER).status_code == 200
    assert owner.get(f"/api/runs/{run_id}/report.md", headers=_OWNER).status_code == 200
    assert (
        sum(
            task.task_type == "notification.email"
            for task in store.list_tasks(run_id, db_path=isolated_db)
        )
        == 0
    )
