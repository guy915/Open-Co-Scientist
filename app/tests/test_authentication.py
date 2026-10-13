from __future__ import annotations

from typing import Any, cast

import pytest
from co_scientist.api.operator_access import is_operator
from co_scientist.core.config import settings
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tests._client import create_run as _create_run


@pytest.mark.parametrize("origin_url", ["https://open-coscientist.com"])
def test_client_ids_isolate_runs_across_cors_and_event_streams(
    monkeypatch: pytest.MonkeyPatch,
    isolated_db: str,
    origin_url: str,
) -> None:
    app = _configure_allowlisted_cors(monkeypatch)
    client = TestClient(app)
    origin = {"Origin": origin_url}
    headers_a = {"X-Client-ID": "researcher-a"}
    headers_b = {"X-Client-ID": "researcher-b"}
    created = _create_run(client, "Private researcher goal", headers=headers_a)
    assert created.status_code == 200
    run_id = created.json()["id"]

    owned = client.get(f"/api/runs/{run_id}", headers={**headers_a, **origin})
    assert owned.status_code == 200
    assert owned.headers["access-control-allow-origin"] == origin_url

    runs.update_run_status(run_id, RunStatus.COMPLETED, db_path=isolated_db)
    events = client.get(f"/api/runs/{run_id}/events", headers={**headers_a, **origin})
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert events.headers["access-control-allow-origin"] == origin_url

    assert client.get(f"/api/runs/{run_id}", headers=headers_b).status_code == 404


def _configure_allowlisted_cors(
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    from co_scientist.main import DEFAULT_ALLOWED_ORIGINS, _resolve_cors_config, app
    from fastapi.middleware.cors import CORSMiddleware

    cors = next(
        middleware
        for middleware in app.user_middleware
        if cast(Any, middleware.cls) is CORSMiddleware
    )
    origins, _ = _resolve_cors_config(DEFAULT_ALLOWED_ORIGINS)
    monkeypatch.setitem(cors.kwargs, "allow_origins", origins)
    monkeypatch.setitem(cors.kwargs, "allow_credentials", True)
    monkeypatch.setattr(app, "middleware_stack", None)
    return app


def test_allowed_origin_can_read_ownership_denial(
    monkeypatch: pytest.MonkeyPatch, isolated_db: str
) -> None:
    app = _configure_allowlisted_cors(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    created = _create_run(client, "Private run", headers={"X-Client-ID": "run-owner"})

    run_path = f"/api/runs/{created.json()['id']}"
    response = client.get(
        run_path,
        headers={
            "X-Client-ID": "different-client",
            "Origin": "https://open-coscientist.com",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}
    assert response.headers["access-control-allow-origin"] == ("https://open-coscientist.com")
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "origin" in response.headers["vary"].lower()

    unlisted = client.get(
        run_path,
        headers={
            "X-Client-ID": "different-client",
            "Origin": "https://evil.example",
        },
    )

    assert unlisted.status_code == 404
    assert "access-control-allow-origin" not in unlisted.headers
    client.close()


def _headerless_client() -> TestClient:
    # Identityless clients must not acquire a default identity header.
    from co_scientist.main import app

    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/api/runs", {"json": {"research_goal": "Anonymous goal"}}),
        (
            "post",
            "/api/documents",
            {
                "files": {"file": ("notes.txt", b"private notes", "text/plain")},
                "data": {"consent": "true"},
            },
        ),
        (
            "post",
            "/api/interviews",
            {"json": {"research_challenge": "Anonymous challenge"}},
        ),
    ],
)
def test_headerless_caller_is_refused_and_creates_nothing(
    isolated_db: str, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = getattr(_headerless_client(), method)(path, **kwargs)

    assert response.status_code == 400
    assert "X-Client-ID" in response.json()["detail"]
    assert runs.list_runs(client_id="") == []


@pytest.mark.parametrize("host", ["example.com/#", "example.com/?"])
def test_host_cannot_hide_another_researchers_run(host: str, isolated_db: str) -> None:
    from co_scientist.main import app

    client = TestClient(app)
    created = _create_run(
        client,
        "Explore mitochondrial dynamics in neurons",
        headers={"X-Client-ID": "owner"},
    )
    assert created.status_code == 200
    response = client.get(
        f"/api/runs/{created.json()['id']}",
        headers={"X-Client-ID": "stranger", "Host": host},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("configured_token", "supplied_token", "host", "expected"),
    [
        ("", "", "127.0.0.1", False),
        ("", "", "::1", False),
        ("", "", "localhost", False),
        ("", "", "127.0.0.2", False),
        ("", "", "remote.example", False),
        ("", "", None, False),
        ("", "admin-token", "remote.example", False),
        ("admin-token", "admin-token", "remote.example", True),
        ("admin-token", "admin-token", None, True),
        ("admin-token", "wrong-token", "remote.example", False),
        ("admin-token", "", "remote.example", False),
        ("admin-token", "wrong-token", "127.0.0.1", False),
        ("admin-token", "", "::1", False),
    ],
)
def test_operator_access(
    monkeypatch: pytest.MonkeyPatch,
    configured_token: str,
    supplied_token: str,
    host: str | None,
    expected: bool,
) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", configured_token)
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-logs-token", supplied_token.encode())],
            "client": (host, 50000) if host else None,
        }
    )

    assert is_operator(request) is expected


@pytest.mark.parametrize(
    "header",
    [
        (b"host", b"localhost"),
        (b"x-forwarded-for", b"127.0.0.1"),
        (b"forwarded", b"for=127.0.0.1;host=localhost"),
    ],
)
def test_request_headers_cannot_supply_loopback_address(
    monkeypatch: pytest.MonkeyPatch, header: tuple[bytes, bytes]
) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", "")
    request = Request(
        {
            "type": "http",
            "headers": [header],
            "client": ("remote.example", 50000),
        }
    )

    assert is_operator(request) is False
