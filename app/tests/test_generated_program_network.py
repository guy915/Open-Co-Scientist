from __future__ import annotations

from pathlib import Path

import pytest
from co_scientist.core.config import settings
from co_scientist.main import app
from co_scientist.platform.db.logs import NewLogRecord, append_log
from co_scientist.platform.sandbox.workspace.run_workspace import open_draft_workspace
from fastapi.testclient import TestClient


def test_generated_draft_programs_have_no_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COSCIENTIST_WORKSPACE_DIR", str(tmp_path))
    workspace = open_draft_workspace("synthetic-run", "synthetic-draft")
    assert not workspace.policy.allows_network
    assert workspace.policy.writable_roots == (workspace.root,)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_loopback_without_operator_token_cannot_read_other_owners_logs(
    monkeypatch: pytest.MonkeyPatch, host: str, token: str | None
) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", "synthetic-operator-token")
    append_log(
        NewLogRecord(
            "WARNING", 30, "synthetic", "private-other-owner-canary", client_id="other-owner"
        )
    )
    headers = {"X-Client-ID": "untrusted-program"}
    if token is not None:
        headers["X-Logs-Token"] = token
    client = TestClient(app, client=(host, 12345), headers=headers)
    response = client.get("/api/logs")
    assert response.status_code == 200
    assert "private-other-owner-canary" not in response.text
    assert client.get("/status").json()["probes"] is None


def test_configured_operator_token_retains_diagnostics_and_global_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", "synthetic-operator-token")
    append_log(
        NewLogRecord(
            "WARNING", 30, "synthetic", "private-other-owner-canary", client_id="other-owner"
        )
    )
    client = TestClient(app, headers={"X-Logs-Token": "synthetic-operator-token"})
    assert "private-other-owner-canary" in client.get("/api/logs").text
    assert isinstance(client.get("/status").json()["probes"], dict)
