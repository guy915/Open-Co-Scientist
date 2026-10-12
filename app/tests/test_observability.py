from __future__ import annotations

import io
import json
import logging
import logging.handlers
import queue
from typing import Any

import pytest
from co_scientist.core.byok_scope import ByokCredential, scoped_byok
from co_scientist.domains.research_state import models
from co_scientist.platform.db import logs
from co_scientist.platform.db import retrieval_calls as retrieval
from co_scientist.platform.db.logs import LogFilters
from co_scientist.platform.telemetry.logging_setup import (
    configure_logging,
)
from fastapi.testclient import TestClient

from tests._client import append_log_row, make_client, make_operator_client
from tests._client import create_run as _create_run
from tests._client import make_client as _client
from tests._client import wait_for_status as _wait_status
from tests._hypothesis_ids import pin_hypothesis_ids


def _restore_default_logging() -> None:
    configure_logging()


def test_json_handler_redacts_a_byok_key_from_exception_text() -> None:
    key = "sk-synthetic-json-log-key-12345"
    diagnostic = "provider diagnostic preserved"
    credential = ByokCredential(provider="deepseek", api_key=key, model="deepseek/test")
    try:
        handler = configure_logging()
        stream = io.StringIO()
        handler.stream = stream  # type: ignore[attr-defined]

        with scoped_byok(credential):
            try:
                raise RuntimeError(f"provider echoed {key}; {diagnostic}")
            except RuntimeError:
                logging.getLogger("app.sample").exception("provider failed")

        payload = json.loads(stream.getvalue().splitlines()[-1])
    finally:
        _restore_default_logging()

    assert key not in payload["exc_info"]
    assert "[REDACTED]" in payload["exc_info"]
    assert diagnostic in payload["exc_info"]


def test_uvicorn_lifecycle_and_access_lines_are_json_on_stdout_not_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    root = logging.getLogger()
    root_handlers = list(root.handlers)
    logging.config.dictConfig(LOGGING_CONFIG)
    try:
        handler = configure_logging()
        stream = io.StringIO()
        handler.stream = stream  # type: ignore[attr-defined]

        logging.getLogger("uvicorn.error").info("Application startup complete.")
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/health", "1.1", 200
        )
        try:
            raise RuntimeError("handler exploded")
        except RuntimeError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    finally:
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).handlers.clear()
        root.handlers[:] = root_handlers
        _restore_default_logging()

    assert capsys.readouterr().err == ""
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(r["logger"], r["level"]) for r in records] == [
        ("uvicorn.error", "INFO"),
        ("uvicorn.access", "INFO"),
        ("uvicorn.error", "ERROR"),
    ]
    assert records[1]["message"] == '127.0.0.1:1 - "GET /health HTTP/1.1" 200'
    assert "handler exploded" not in records[2]["exc_info"]
    assert records[2]["exc_info"] == "RuntimeError"


def test_reconfigured_logging_keeps_the_capture_handler_on_uvicorn() -> None:
    capture = logging.handlers.QueueHandler(queue.SimpleQueue())
    access = logging.getLogger("uvicorn.access")
    access.addHandler(capture)
    try:
        configure_logging()
        configure_logging()
        handlers = list(access.handlers)
    finally:
        access.removeHandler(capture)
        _restore_default_logging()

    assert capture in handlers
    assert sum(isinstance(h, logging.StreamHandler) for h in handlers) == 1


def _logs_endpoint_seed(
    isolated_db: str,
    message: str,
    *,
    level: str = "INFO",
    levelno: int = logging.INFO,
    run_id: str | None = None,
) -> int:
    return append_log_row(isolated_db, message, level=level, levelno=levelno, run_id=run_id)


def test_logs_endpoint_returns_rows_and_last_id(isolated_db: str) -> None:
    first = _logs_endpoint_seed(isolated_db, "first line")
    last = _logs_endpoint_seed(isolated_db, "second line")
    response = make_operator_client().get("/api/logs")
    assert response.status_code == 200
    body = response.json()
    messages = [row["message"] for row in body["logs"]]
    assert "first line" in messages
    assert "second line" in messages
    assert body["last_id"] >= last
    row = body["logs"][messages.index("first line")]
    assert row["id"] == first
    assert row["level"] == "INFO"
    assert row["logger"] == "app.seeded"

    _logs_endpoint_seed(isolated_db, "new line")
    polled = make_operator_client().get("/api/logs", params={"after_id": body["last_id"]}).json()
    assert "new line" in [row["message"] for row in polled["logs"]]
    assert all(row["id"] > body["last_id"] for row in polled["logs"])


# Shared logs contain other tenants and server internals; remote reads need
# ownership or operator access.


def _security_seed(
    isolated_db: str,
    message: str,
    *,
    run_id: str | None = None,
    client_id: str | None = None,
) -> int:
    return append_log_row(isolated_db, message, run_id=run_id, client_id=client_id)


# TestClient reports a non-loopback host, exercising remote policy unless an
# operator token is supplied.


def _admin_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    from co_scientist.core.config import settings

    monkeypatch.setattr(settings, "logs_admin_token", token)


def test_remote_read_is_scoped_to_the_caller(isolated_db: str) -> None:
    _security_seed(isolated_db, "alice ui record", client_id="alice")
    _security_seed(isolated_db, "bob ui record", client_id="bob")
    _security_seed(isolated_db, "server internals")
    client = make_client()

    body = client.get("/api/logs", headers={"X-Client-ID": "alice"}).json()
    messages = [r["message"] for r in body["logs"]]
    assert messages == ["alice ui record"]
    assert body["total"] == 1


def test_ingested_records_are_stamped_with_the_caller_and_sanitized(
    isolated_db: str,
) -> None:
    client = make_client()
    client.post(
        "/api/logs",
        json={
            "records": [
                {
                    "message": "real\n2026-01-01\tINFO\tapp.fake\tforged",
                    "logger": "sess\nion",
                }
            ]
        },
        headers={"X-Client-ID": "alice"},
    )
    alice = logs.list_logs(filters=LogFilters(scope_client_id="alice"), db_path=isolated_db)
    assert len(alice) == 1
    assert "\n" not in alice[0]["message"] + alice[0]["logger"]
    assert "\t" not in alice[0]["message"]
    assert logs.list_logs(filters=LogFilters(scope_client_id="bob"), db_path=isolated_db) == []


def test_remote_read_without_identity_sees_nothing(isolated_db: str) -> None:
    _security_seed(isolated_db, "alice ui record", client_id="alice")
    _security_seed(isolated_db, "server internals")
    body = make_client().get("/api/logs").json()
    assert body["logs"] == []


def test_admin_token_grants_the_app_wide_view(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _admin_token(monkeypatch, "s3cret")
    _security_seed(isolated_db, "alice ui record", client_id="alice")
    _security_seed(isolated_db, "server internals")
    client = make_client()

    body = client.get("/api/logs", headers={"X-Logs-Token": "s3cret"}).json()
    assert len(body["logs"]) == 2
    body = client.get("/api/logs", headers={"X-Logs-Token": "nope"}).json()
    assert body["logs"] == []


def test_ingestion_is_rate_limited(isolated_db: str) -> None:
    client = make_client()
    headers = {"X-Client-ID": "flooder"}
    last = None
    for _ in range(200):
        last = client.post(
            "/api/logs",
            json={"records": [{"message": "spam"}]},
            headers=headers,
        )
        if last.status_code == 429:
            break
    assert last is not None and last.status_code == 429


_METRIC_FIELDS = (
    "total_time",
    "hypothesis_count",
    "reviews_count",
    "tournaments_count",
    "evolutions_count",
    "llm_calls",
    "phase_times",
)


def _run_to_completion(client: TestClient, goal: str) -> dict[str, Any]:
    created = _create_run(client, goal, tier="express")
    run_id = created.json()["id"]
    started = client.post(f"/api/runs/{run_id}/start", json={})
    assert started.status_code == 200
    assert _wait_status(client, run_id, "completed", timeout=30.0)

    metrics = retrieval.get_run_metrics(run_id)
    assert metrics is not None
    return dict(metrics)


def test_completed_run_serves_engine_metrics(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Random hypothesis ids shape the offline prompts, so some runs drew no
    # evolved child; a pinned id sequence makes the run reproducible.
    pin_hypothesis_ids(monkeypatch, models, "metrics-run")
    metrics = _run_to_completion(_client(), "Metrics for a full run")

    for field in _METRIC_FIELDS:
        assert field in metrics
    assert metrics["hypothesis_count"] >= 1
    assert metrics["tournaments_count"] >= 1
    assert metrics["evolutions_count"] > 0
    assert metrics["llm_calls"] > 0
    assert isinstance(metrics["phase_times"], dict)
    assert metrics["total_time"] >= 0.0
