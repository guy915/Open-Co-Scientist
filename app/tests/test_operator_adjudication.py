from __future__ import annotations

import pytest
from co_scientist.core.config import settings
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.records import NewSafetyDecision
from co_scientist.domains.safety.gate import POLICY_VERSION
from co_scientist.main import app
from fastapi.testclient import TestClient

from tests._store_helpers import seed_run


def _held_run() -> tuple[str, int]:
    run = seed_run("Review research context", client_id="owner", llm_backend="offline")
    records.add_safety_decision(
        NewSafetyDecision(
            run_id=run.id,
            stage="intake",
            decision="hold",
            reason="Independent review required",
            matches=[],
            policy_version=POLICY_VERSION,
            requires_review=True,
        )
    )
    return run.id, int(records.list_safety_decisions(run.id)[0]["id"])


@pytest.mark.parametrize("resolution", ["approved", "rejected"])
@pytest.mark.parametrize("peer", ["testclient", "127.0.0.1"])
def test_owner_cannot_adjudicate_without_operator_token(
    monkeypatch: pytest.MonkeyPatch,
    resolution: str,
    peer: str,
) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", "synthetic-admin")
    run_id, decision_id = _held_run()
    client = TestClient(app, client=(peer, 50000), headers={"X-Client-ID": "owner"})
    response = client.post(
        f"/api/runs/{run_id}/safety/{decision_id}/adjudicate",
        json={"resolution": resolution},
    )
    assert response.status_code == 403
    decision = records.list_safety_decisions(run_id)[0]
    assert decision["resolution"] is None
    assert decision["resolved_by"] is None
    assert not records.safety_stage_is_approved(run_id, "intake", POLICY_VERSION)


def test_configured_operator_can_adjudicate_without_owner_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", "synthetic-admin")
    run_id, decision_id = _held_run()
    client = TestClient(app, headers={"X-Logs-Token": "synthetic-admin"})
    response = client.post(
        f"/api/runs/{run_id}/safety/{decision_id}/adjudicate",
        json={"resolution": "approved"},
    )
    assert response.status_code == 200
    decision = records.list_safety_decisions(run_id)[0]
    assert decision["resolution"] == "approved"
    assert decision["resolved_by"] == "operator"
    assert records.safety_stage_is_approved(run_id, "intake", POLICY_VERSION)
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 404


@pytest.mark.parametrize("configured,supplied", [("", ""), ("synthetic-admin", "wrong")])
def test_unconfigured_or_wrong_operator_token_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    supplied: str,
) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", configured)
    run_id, decision_id = _held_run()
    client = TestClient(app, headers={"X-Client-ID": "owner", "X-Logs-Token": supplied})
    assert (
        client.post(
            f"/api/runs/{run_id}/safety/{decision_id}/adjudicate",
            json={"resolution": "approved"},
        ).status_code
        == 403
    )
    assert records.list_safety_decisions(run_id)[0]["resolution"] is None
