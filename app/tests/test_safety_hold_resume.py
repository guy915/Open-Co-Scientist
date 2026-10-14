# Succeeded tasks cannot be re-enqueued under existing idempotency keys; holds
# must park claimable work.

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from co_scientist.core.config import settings
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.records import NewSafetyDecision
from co_scientist.domains.safety.gate import POLICY_VERSION, SafetyDecision, ScreenSubject
from co_scientist.orchestration import task_worker
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus
from fastapi.testclient import TestClient

from tests._client import create_run as _create_run
from tests._client import make_client
from tests._client import make_client as _client
from tests._engine_tasks_helpers import (
    _install_runtime,
)
from tests._store_helpers import (
    enqueue_task,
    seed_run,
)

CLIENT_ID = "hold-e2e"
ADMIN_HEADERS = {"X-Logs-Token": "test-safety-operator"}
HEADERS = {"X-Client-ID": CLIENT_ID}


@pytest.fixture(autouse=True)
def operator_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", ADMIN_HEADERS["X-Logs-Token"])


def _held_decision(stage: str) -> SafetyDecision:
    return SafetyDecision(
        stage=stage,
        decision="hold",
        reason="Contextual safety assessment was uncertain.",
        category="uncertain",
        risk_domains=["uncertain"],
        requires_review=True,
        assessor="semantic:test",
    )


def hold_until_approved(stage: str) -> Any:
    # Reviewer approval bypasses escalation, so this stub must hold only until
    # that stage is approved.

    async def _screen(
        run_id: str,
        subject: ScreenSubject,
        *,
        provider: str,
        db_path: str | None = None,
    ) -> SafetyDecision:
        approved = records.safety_stage_is_approved(run_id, stage, POLICY_VERSION, db_path=db_path)
        if subject.stage != stage or approved:
            return subject.deterministic
        return _held_decision(stage)

    return _screen


def start_offline_run(db_path: str) -> Any:
    run = seed_run(
        "Explain how protein X folds under crowding.",
        profile="express",
        config={"tier": "express", "enable_literature_review": False},
        client_id=CLIENT_ID,
        llm_backend="offline",
        db_path=db_path,
    )
    task_worker.enqueue_run_workflow(run.id, db_path=db_path)
    return run


def drain(run_id: str, db_path: str, worker: str = "hold-e2e") -> None:
    asyncio.run(
        task_worker.run_run_worker_pool(
            run_id,
            worker,
            policy=task_worker.WorkerPolicy(db_path=db_path),
        )
    )


def held_decision_id(run_id: str, stage: str, db_path: str) -> int:
    held = [
        row
        for row in records.list_safety_decisions(run_id, db_path=db_path)
        if row["stage"] == stage and row["decision"] == "hold" and row["resolution"] is None
    ]
    assert held, f"no unresolved {stage} hold was recorded"
    return int(held[-1]["id"])


def claimable_engine_tasks(run_id: str, db_path: str) -> list[str]:
    return [
        task.task_type
        for task in tasks.list_tasks(run_id, db_path=db_path)
        if task.task_type.startswith("engine.") and task.status in {"queued", "leased", "paused"}
    ]


def approve(client: TestClient, run_id: str, decision_id: int) -> None:
    response = client.post(
        f"/api/runs/{run_id}/safety/{decision_id}/adjudicate",
        headers={**HEADERS, **ADMIN_HEADERS},
        json={"resolution": "approved"},
    )
    assert response.status_code == 200, response.text


@pytest.fixture()
def held_run(manual_worker: None, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Disable detached workers to avoid racing the cohort this test drains
    # directly.

    monkeypatch.setattr(settings, "semantic_safety_enabled", False)
    return make_client()


@pytest.mark.parametrize(
    ("stage", "boundary"),
    [("intake", "engine.bootstrap"), ("final", "engine.finalize")],
)
def test_a_hold_keeps_its_boundary_claimable_and_resumes_on_approval(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    held_run: TestClient,
    stage: str,
    boundary: str,
) -> None:
    _install_runtime(monkeypatch).screen = hold_until_approved(stage)
    run = start_offline_run(isolated_db)
    drain(run.id, isolated_db)

    paused = runs.get_run(run.id, db_path=isolated_db)
    assert paused is not None and paused.status == RunStatus.PAUSED.value
    assert reports.get_latest_report(run.id, db_path=isolated_db) is None
    # A hold is waiting work; its boundary must remain queued for reviewer
    # release.
    assert claimable_engine_tasks(run.id, isolated_db) == [boundary]

    approve(held_run, run.id, held_decision_id(run.id, stage, isolated_db))
    drain(run.id, isolated_db, worker="hold-e2e-resume")

    final = runs.get_run(run.id, db_path=isolated_db)
    assert final is not None
    assert final.status == RunStatus.COMPLETED.value
    assert reports.get_latest_report(run.id, db_path=isolated_db) is not None


def test_parked_task_is_released_with_a_fresh_budget(
    isolated_db: str,
) -> None:
    # Waiting for a reviewer must preserve claimable work without spending its
    # retry budget.
    run = seed_run("goal", profile="express", db_path=isolated_db)
    enqueue_task(run.id, "engine.finalize", "engine.finalize:1", db_path=isolated_db)
    leased = tasks.claim_task("w1", run_id=run.id, db_path=isolated_db)
    assert leased is not None and leased.attempt == 1

    assert lifecycle.park_task(leased.id, "w1", "held", db_path=isolated_db)
    parked = tasks.get_task(leased.id, db_path=isolated_db)
    assert parked is not None and parked.status == "paused"
    assert tasks.claim_task("w2", run_id=run.id, db_path=isolated_db) is None

    assert lifecycle.resume_run_tasks(run.id, db_path=isolated_db) == 1
    released = tasks.claim_task("w3", run_id=run.id, db_path=isolated_db)
    assert released is not None and released.id == leased.id
    assert released.attempt == 1


def test_rejected_final_hold_blocks_the_run(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch, held_run: TestClient
) -> None:
    _install_runtime(monkeypatch).screen = hold_until_approved("final")
    run = start_offline_run(isolated_db)
    drain(run.id, isolated_db)

    decision_id = held_decision_id(run.id, "final", isolated_db)
    response = held_run.post(
        f"/api/runs/{run.id}/safety/{decision_id}/adjudicate",
        headers={**HEADERS, **ADMIN_HEADERS},
        json={"resolution": "rejected"},
    )
    assert response.status_code == 200

    blocked = runs.get_run(run.id, db_path=isolated_db)
    assert blocked is not None
    assert blocked.status == RunStatus.BLOCKED.value
    assert reports.get_latest_report(run.id, db_path=isolated_db) is None


@pytest.mark.parametrize(
    ("paused", "held", "resolved", "awaiting"),
    [
        (True, True, False, 1),
        (True, False, False, 0),
        (False, True, False, 0),
        (True, True, True, 0),
    ],
)
def test_only_a_paused_run_with_an_unresolved_review_awaits_a_decision(
    isolated_db: str, paused: bool, held: bool, resolved: bool, awaiting: int
) -> None:
    client = _client()
    headers = {"X-Client-ID": "awaiting"}
    if held:
        run_id, decision_id = _run_with_held_decision(client, headers)
    else:
        run_id = _create_run(client, "A mundane pathway", headers=headers).json()["id"]
    if resolved:
        client.post(
            f"/api/runs/{run_id}/safety/{decision_id}/adjudicate",
            headers={**headers, **ADMIN_HEADERS},
            json={"resolution": "approved"},
        )
    if paused:
        runs.update_run_status(run_id, RunStatus.PAUSED)

    detail = client.get(f"/api/runs/{run_id}", headers=headers).json()

    assert detail["awaiting_decision_count"] == awaiting


def _run_with_held_decision(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:

    created = _create_run(client, "Review a sensitive research protocol", headers=headers).json()
    records.add_safety_decision(
        NewSafetyDecision(
            run_id=created["id"],
            stage="intake",
            decision="hold",
            reason="Context requires review.",
            matches=[],
            category="uncertain",
            policy_version="coscientist-safety-v2",
            requires_review=True,
        )
    )
    decision_id = records.list_safety_decisions(created["id"])[0]["id"]
    return created["id"], decision_id


def test_held_hypothesis_adjudication_records_without_blocking(
    isolated_db: str,
) -> None:
    # Hypothesis-stage holds resolve one excluded idea without restarting or
    # stopping the whole run.
    from tests._drain_helpers import _held_final_state, _persist

    client = _client()
    headers = {"X-Client-ID": "held-reviewer"}
    created = _create_run(client, "Adjudicate hypotheses held for review", headers=headers).json()
    run_id = created["id"]
    _persist(run_id=run_id, final_state=_held_final_state(), db_path=isolated_db)

    listed = client.get(f"/api/runs/{run_id}/safety", headers=headers)
    assert listed.status_code == 200
    holds = [d for d in listed.json()["safety"] if d["decision"] == "hold"]
    assert len(holds) == 2

    approved = client.post(
        f"/api/runs/{run_id}/safety/{holds[0]['id']}/adjudicate",
        headers={**headers, **ADMIN_HEADERS},
        json={"resolution": "approved"},
    )
    assert approved.status_code == 200

    rejected = client.post(
        f"/api/runs/{run_id}/safety/{holds[1]['id']}/adjudicate",
        headers={**headers, **ADMIN_HEADERS},
        json={"resolution": "rejected"},
    )
    assert rejected.status_code == 200

    by_id = {
        d["id"]: d
        for d in client.get(f"/api/runs/{run_id}/safety", headers=headers).json()["safety"]
    }
    assert by_id[holds[0]["id"]]["resolution"] == "approved"
    assert by_id[holds[1]["id"]]["resolution"] == "rejected"
    repeated = client.post(
        f"/api/runs/{run_id}/safety/{holds[0]['id']}/adjudicate",
        headers={**headers, **ADMIN_HEADERS},
        json={"resolution": "rejected"},
    )
    assert repeated.status_code == 409
    assert client.get(f"/api/runs/{run_id}", headers=headers).json()["status"] == "draft"


def test_approval_does_not_replay_credit_rejection_during_an_existing_safety_pause(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch, held_run: TestClient
) -> None:
    from co_scientist.platform.db import transaction
    from co_scientist.platform.db.models import TaskFailure

    _install_runtime(monkeypatch).screen = hold_until_approved("intake")
    run = start_offline_run(isolated_db)
    drain(run.id, isolated_db)
    sibling = enqueue_task(run.id, "engine.node.research_overview", "credit-sibling")
    with transaction(isolated_db) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET status='leased', lease_owner='sibling', "
            "attempt=1 WHERE id=?",
            (sibling.id,),
        )
    assert lifecycle.park_task_for_recovery(
        sibling.id,
        "sibling",
        TaskFailure("Provider API credits exhausted", "llm_credit_exhausted"),
        db_path=isolated_db,
    )
    approve(held_run, run.id, held_decision_id(run.id, "intake", isolated_db))
    saved = runs.get_run(run.id, db_path=isolated_db)
    assert saved is not None and saved.status == "paused"
    assert not tasks.claim_task("unconfirmed", run_id=run.id, db_path=isolated_db)
    detail = held_run.get(f"/api/runs/{run.id}", headers=HEADERS).json()
    assert detail["can_resume"] is True
    assert detail["failure_kind"] == "llm_credit_exhausted"
