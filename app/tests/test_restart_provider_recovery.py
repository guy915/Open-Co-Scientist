from __future__ import annotations

from typing import Any

import pytest
from co_scientist.domains.research_state.repository import records
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.repository import events, tasks
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import (
    UNKNOWN_PROVIDER_OUTCOME_ERROR,
    RunStatus,
    ScientificTask,
)

from tests._client import DEFAULT_TEST_CLIENT_ID, make_client
from tests._store_helpers import enqueue_task, seed_checkpoint, seed_run


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["engine.bootstrap", "engine.node.research_overview"])
@pytest.mark.parametrize("zero_cost", [False, True])
async def test_restart_preserves_an_exhausted_boundary_until_acknowledged(
    manual_worker: None,
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
    zero_cost: bool,
) -> None:
    dispatched: list[str] = []

    async def execute(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        dispatched.append(task.id)
        return {}

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", execute)
    run = seed_run(
        "Preserve restart work",
        config={"zero_cost_admission": zero_cost},
        options=runs.RunCreateOptions(client_id=DEFAULT_TEST_CLIENT_ID),
    )
    runs.update_run_status(run.id, RunStatus.RUNNING)
    if task_type != "engine.bootstrap":
        seed_checkpoint(
            run.id, {"provider": "engine", "resume_successor": task_type}, stage="post_generation"
        )
    target = enqueue_task(
        run.id,
        task_type,
        f"{task_type}:1" if task_type != "engine.bootstrap" else "engine:bootstrap:v1",
        inputs={"checkpoint_seq": 1},
        max_attempts=1,
    )
    leased = tasks.claim_task("previous-process", run_id=run.id, lease_seconds=30)
    assert leased is not None and leased.id == target.id
    tasks.expire_earlier_process_leases("new-process")

    with make_client() as client:
        body = client.get(f"/api/runs/{run.id}").json()
        assert body["status"] == "paused"
        assert body["can_resume"] is True
        assert body["failure_kind"] == "llm_interrupted_unknown"
        assert not await task_worker.run_once("automatic", run_id=run.id)
        paused = tasks.get_task(target.id)
        assert paused is not None and paused.attempt == 1 and paused.max_attempts == 2
        assert len(paused.attempts) == 1
        assert not tasks.complete_task(target.id, "previous-process", {})
    with make_client() as restarted:
        assert restarted.get(f"/api/runs/{run.id}").json()["can_resume"] is True
        assert dispatched == []
        resumed = restarted.post(
            f"/api/runs/{run.id}/resume", json={"acknowledge_unknown_outcome": True}
        )
        assert resumed.status_code == 200
        assert resumed.json()["task_id"] == target.id
        assert await task_worker.run_once("owner", run_id=run.id)
    assert dispatched == [target.id]
    assert not any(
        event["type"] == "lifecycle" and event["payload"].get("event") == "legacy_resume_cleanup"
        for event in events.list_events(run.id)
    )


@pytest.mark.parametrize("item_first", [False, True])
def test_mixed_expired_leases_pause_once_without_cancelling_work(
    manual_worker: None, isolated_db: str, item_first: bool
) -> None:
    run = seed_run("Concurrent interrupted work")
    runs.update_run_status(run.id, RunStatus.RUNNING)
    types = [
        "engine.node.research_overview",
        "engine.node.meta_review",
        "engine.fanout.review.item",
    ]
    if item_first:
        types.reverse()
    seeded = [enqueue_task(run.id, kind, kind) for kind in types]
    for index in range(len(seeded)):
        assert tasks.claim_task(f"old-{index}", run_id=run.id)
    successor = enqueue_task(run.id, "engine.node.orchestrator", "next")
    assert tasks.expire_earlier_process_leases("new-process") == 3
    saved = runs.get_run(run.id)
    assert saved is not None and saved.status == "paused"
    saved_successor = tasks.get_task(successor.id)
    assert saved_successor is not None and saved_successor.status == "paused"
    for task in seeded:
        row = tasks.get_task(task.id)
        assert row is not None
        assert row.status == ("failed" if task.task_type.endswith(".item") else "paused")
        assert row.lease_owner is None
    paused = [
        e
        for e in events.list_events(run.id)
        if e["type"] == "status" and e["payload"].get("status") == "paused"
    ]
    assert len(paused) == 1


def test_historical_unknown_outcome_can_resume_only_with_checkpoint_and_acknowledgement(
    manual_worker: None, isolated_db: str
) -> None:
    with make_client() as client:
        run = seed_run(
            "Historical interruption",
            options=runs.RunCreateOptions(client_id=DEFAULT_TEST_CLIENT_ID),
        )
        runs.update_run_status(run.id, RunStatus.FAILED, error=UNKNOWN_PROVIDER_OUTCOME_ERROR)
        events.append_event(
            run.id, "status", {"status": "failed", "failure_kind": "llm_timeout_unknown"}
        )
        path = f"/api/runs/{run.id}/resume"
        assert client.post(path, json={"acknowledge_unknown_outcome": True}).status_code == 409
        seed_checkpoint(
            run.id,
            {"provider": "engine", "resume_successor": "engine.node.research_overview"},
            stage="post_generation",
        )
        target = enqueue_task(
            run.id,
            "engine.node.research_overview",
            "engine.node.research_overview:1",
            inputs={"checkpoint_seq": 1},
        )
        with db.transaction() as conn:
            conn.execute(
                "UPDATE scientific_tasks SET status='failed', error=? WHERE id=?",
                (UNKNOWN_PROVIDER_OUTCOME_ERROR, target.id),
            )
        assert client.get(f"/api/runs/{run.id}").json()["can_resume"] is True
        assert client.post(path, json={}).status_code == 409
        response = client.post(path, json={"acknowledge_unknown_outcome": True})
        assert response.status_code == 200
        assert response.json()["task_id"] == target.id
        saved_target = tasks.get_task(target.id)
        assert saved_target is not None and saved_target.status == "queued"


def test_acknowledgement_classifies_sibling_leases_that_expired_while_paused(
    manual_worker: None, isolated_db: str
) -> None:
    run = seed_run("Sibling expires", client_id=DEFAULT_TEST_CLIENT_ID)
    runs.update_run_status(run.id, RunStatus.RUNNING)
    seed_checkpoint(
        run.id,
        {"provider": "engine", "resume_successor": "engine.node.research_overview"},
        stage="post_generation",
    )
    target = enqueue_task(run.id, "engine.node.research_overview", "overview")
    sibling = enqueue_task(run.id, "engine.node.meta_review", "sibling")
    assert tasks.claim_task("old-process", run_id=run.id)
    assert tasks.claim_task(f"worker.{task_worker.process_tag()}:sibling", run_id=run.id)
    tasks.expire_earlier_process_leases(task_worker.process_tag())
    with make_client() as client:
        assert client.get(f"/api/runs/{run.id}").json()["can_resume"] is False
        with db.transaction() as conn:
            conn.execute("UPDATE scientific_tasks SET lease_expires_at=0 WHERE id=?", (sibling.id,))
        response = client.post(
            f"/api/runs/{run.id}/resume", json={"acknowledge_unknown_outcome": True}
        )
        assert response.status_code == 200
    for original in (target, sibling):
        saved = tasks.get_task(original.id)
        assert saved is not None and saved.status == "queued" and saved.lease_owner is None
    assert tasks.claim_task("resumed", run_id=run.id) is not None
    saved_run = runs.get_run(run.id)
    assert saved_run is not None and saved_run.status != "paused"


def test_interruption_resume_cannot_bypass_an_unresolved_safety_hold(
    manual_worker: None, isolated_db: str
) -> None:
    with make_client() as client:
        run = seed_run("Pending safety review", client_id=DEFAULT_TEST_CLIENT_ID)
        runs.update_run_status(run.id, RunStatus.PAUSED)
        events.append_event(
            run.id, "status", {"status": "paused", "failure_kind": "llm_interrupted_unknown"}
        )
        records.add_safety_decision(
            records.NewSafetyDecision(
                run_id=run.id,
                stage="intake",
                decision="review",
                reason="Requires review",
                matches=[],
                requires_review=True,
            )
        )
        assert client.get(f"/api/runs/{run.id}").json()["can_resume"] is False
        assert (
            client.post(
                f"/api/runs/{run.id}/resume", json={"acknowledge_unknown_outcome": True}
            ).status_code
            == 409
        )
    saved = runs.get_run(run.id)
    assert saved is not None and saved.status == "paused"


@pytest.mark.parametrize("status", [RunStatus.PAUSED, RunStatus.FAILED])
@pytest.mark.parametrize("checkpoint", [False, True])
def test_credit_rejection_can_resume_the_preserved_boundary_without_unknown_ack(
    manual_worker: None, isolated_db: str, status: RunStatus, checkpoint: bool
) -> None:
    run = seed_run("Top up and resume", client_id=DEFAULT_TEST_CLIENT_ID)
    task_type = "engine.node.research_overview" if checkpoint else "engine.bootstrap"
    key = f"{task_type}:1" if checkpoint else "engine:bootstrap:v1"
    target = enqueue_task(run.id, task_type, key, max_attempts=1)
    if checkpoint:
        seed_checkpoint(
            run.id, {"provider": "engine", "resume_successor": task_type}, stage="saved"
        )
    runs.update_run_status(run.id, status, error="Provider API credits exhausted")
    events.append_event(
        run.id, "status", {"status": status.value, "failure_kind": "llm_credit_exhausted"}
    )
    with db.transaction() as conn:
        conn.execute(
            "UPDATE scientific_tasks SET status=?, attempt=1 WHERE id=?", (status.value, target.id)
        )
    with make_client() as client:
        detail = client.get(f"/api/runs/{run.id}").json()
        assert detail["can_resume"] is True
        assert detail["resume_requires_ack"] is False
        response = client.post(f"/api/runs/{run.id}/resume", json={})
        assert response.status_code == 200, response.json()
        assert response.json()["task_id"] == target.id
    resumed = tasks.get_task(target.id)
    assert resumed is not None and resumed.status == "queued"
    assert resumed.attempt == 1 and resumed.max_attempts == 2


def test_call_ceiling_failure_does_not_offer_credit_recovery(manual_worker: None) -> None:
    run = seed_run("Budget spent", client_id=DEFAULT_TEST_CLIENT_ID)
    runs.update_run_status(run.id, RunStatus.FAILED)
    seed_checkpoint(run.id, {"provider": "engine"}, stage="saved")
    events.append_event(
        run.id, "status", {"status": "failed", "failure_kind": "llm_call_budget_exceeded"}
    )
    with make_client() as client:
        assert client.get(f"/api/runs/{run.id}").json()["can_resume"] is False
        assert client.post(f"/api/runs/{run.id}/resume", json={}).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_type", ["engine.bootstrap", "engine.node.research_overview", "engine.fanout.review.item"]
)
async def test_credit_exhaustion_parks_without_losing_or_replaying_funded_work(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch, task_type: str
) -> None:
    from co_scientist.core.exceptions import ProviderCreditExhaustedError

    calls: list[str] = []

    async def execute(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        calls.append(task.id)
        raise ProviderCreditExhaustedError("Provider API credits exhausted")

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", execute)
    run = seed_run("Credit exhausted", client_id=DEFAULT_TEST_CLIENT_ID)
    runs.update_run_status(run.id, RunStatus.RUNNING)
    target = enqueue_task(run.id, task_type, "credit-target", max_attempts=1)
    downstream = enqueue_task(
        run.id, "engine.node.orchestrator", "after-credit", dependencies=(target.id,)
    )
    assert await task_worker.run_once("credit-owner", run_id=run.id)
    assert not await task_worker.run_once("automatic-retry", run_id=run.id)
    saved = tasks.get_task(target.id)
    assert saved is not None and saved.status == "paused"
    assert saved.attempt == 1 and saved.max_attempts == 2
    assert len(saved.attempts) == 1 and saved.lease_owner is None
    saved_downstream = tasks.get_task(downstream.id)
    saved_run = runs.get_run(run.id)
    assert saved_downstream is not None and saved_downstream.status == "paused"
    assert saved_run is not None and saved_run.status == "paused"
    assert not tasks.complete_task(target.id, "credit-owner", {})
    with make_client() as client:
        detail = client.get(f"/api/runs/{run.id}").json()
        assert detail["can_resume"] is True
        assert detail["failure_kind"] == "llm_credit_exhausted"
    assert calls == [target.id]


@pytest.mark.asyncio
async def test_permanent_admission_invariant_does_not_become_resumable(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.core.exceptions import ProviderAdmissionError

    async def execute(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        raise ProviderAdmissionError("Unsupported request shape")

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", execute)
    run = seed_run("Invalid dispatch", client_id=DEFAULT_TEST_CLIENT_ID)
    runs.update_run_status(run.id, RunStatus.RUNNING)
    enqueue_task(run.id, "engine.bootstrap", "engine:bootstrap:v1", max_attempts=1)
    assert await task_worker.run_once("owner", run_id=run.id)
    with make_client() as client:
        detail = client.get(f"/api/runs/{run.id}").json()
        assert detail["status"] == "failed"
        assert detail["can_resume"] is False
