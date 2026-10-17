from __future__ import annotations

import sqlite3

import pytest
from co_scientist.api import usage_api
from co_scientist.core.config import settings
from co_scientist.platform.db import connect
from fastapi import FastAPI
from fastapi.testclient import TestClient

_NOW = 20_000 * 86400 + 3600.0
_OPERATOR = {"X-Logs-Token": "test-operator"}


def _chat(conn: sqlite3.Connection, ident: str, client_id: str, at: float) -> None:
    conn.execute(
        "INSERT INTO interviews (id, client_id, status, fields_json, created_at, updated_at)"
        " VALUES (?, ?, 'active', '{}', ?, ?)",
        (ident, client_id, at, at),
    )


def _run(
    conn: sqlite3.Connection, ident: str, client_id: str, at: float, provider: str = "engine"
) -> None:
    conn.execute(
        "INSERT INTO runs (id, research_goal, profile, status, provider, config_json,"
        " client_id, created_at, updated_at)"
        " VALUES (?, 'g', 'express', 'queued', ?, '{}', ?, ?, ?)",
        (ident, provider, client_id, at, at),
    )


@pytest.fixture
def client(isolated_db: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "logs_admin_token", "test-operator")
    monkeypatch.setattr(usage_api, "current_time", lambda: _NOW)
    app = FastAPI()
    app.include_router(usage_api.router)
    return TestClient(app)


def test_usage_is_hidden_without_the_operator_token(client: TestClient) -> None:
    assert client.get("/api/usage").status_code == 404
    assert client.get("/api/usage", headers={"X-Logs-Token": "wrong"}).status_code == 404


def test_usage_counts_distinct_browsers_chats_and_runs_per_day(client: TestClient) -> None:
    yesterday = _NOW - 86400
    with connect() as conn:
        _chat(conn, "c1", "alice", yesterday)
        _run(conn, "r1", "alice", yesterday)
        _chat(conn, "c2", "bob", yesterday)
        _chat(conn, "c3", "alice", _NOW)
        _run(conn, "r2", "carol", _NOW)
        _run(conn, "r3", "carol", _NOW)
        _run(conn, "mock", "dave", _NOW, provider="mock")
        _chat(conn, "anon", "", _NOW)
        _chat(conn, "old", "erin", _NOW - 5 * 86400)

    reply = client.get("/api/usage", params={"days": 2}, headers=_OPERATOR)

    assert reply.status_code == 200
    assert reply.headers["cache-control"] == "no-store"
    body = reply.json()
    assert body["since"] == "2024-10-03"
    assert (body["active_clients"], body["chats_started"], body["runs_created"]) == (3, 3, 3)
    assert body["run_clients"] == 2
    assert body["daily"] == [
        {
            "date": "2024-10-03",
            "active_clients": 2,
            "chats_started": 2,
            "runs_created": 1,
            "run_clients": 1,
        },
        {
            "date": "2024-10-04",
            "active_clients": 2,
            "chats_started": 1,
            "runs_created": 2,
            "run_clients": 1,
        },
    ]


def test_usage_fills_quiet_days_with_zeros(client: TestClient) -> None:
    body = client.get("/api/usage", params={"days": 3}, headers=_OPERATOR).json()
    assert [day["active_clients"] for day in body["daily"]] == [0, 0, 0]
    assert body["active_clients"] == 0


@pytest.mark.parametrize("days", [0, 367])
def test_usage_rejects_windows_outside_one_year(client: TestClient, days: int) -> None:
    assert client.get("/api/usage", params={"days": days}, headers=_OPERATOR).status_code == 422
