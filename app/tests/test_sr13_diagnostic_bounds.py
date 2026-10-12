from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from co_scientist.core.config import settings
from co_scientist.platform.db import log_admission, logs
from co_scientist.platform.db import log_capture as logging_setup
from co_scientist.platform.db.logs import NewLogRecord

from tests._client import make_client


@pytest.fixture(autouse=True)
def _isolated_ingest_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    from co_scientist.api import logs_api

    # Database admission is already isolated by the shared fixture. Keep the
    # process-local host/identity window equally independent between cases.
    monkeypatch.setattr(logs_api, "_ingest_hits", {})


def _record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("app.synthetic", level, __file__, 1, message, (), None)


def test_stalled_capture_has_a_finite_record_and_byte_backlog() -> None:
    handler, listener = logging_setup._build_capture_pipeline(logging.INFO, 20_000)
    for index in range(1024):
        handler.handle(_record(f"synthetic-{index}: " + "x" * 8192))
    assert listener.queue.stats()["queued_records"] <= 512


def test_capture_drop_counters_and_byte_capacity_are_observable() -> None:
    handler, listener = logging_setup._build_capture_pipeline(logging.INFO, 20_000)
    for index in range(1024):
        handler.handle(_record(f"synthetic-{index}: " + "x" * 8192))
    stats = listener.queue.stats()
    assert stats["queued_records"] <= 512
    assert stats["queued_bytes"] <= 8 * 1024**2
    assert stats["dropped_normal"] >= 512
    assert stats["dropped_critical"] == 0


def test_warning_capacity_is_reserved_and_drained_before_ordinary_chatter() -> None:
    handler, listener = logging_setup._build_capture_pipeline(logging.INFO, 20_000)
    for index in range(512):
        handler.handle(_record(f"ordinary-{index}"))
    handler.handle(_record("critical synthetic canary", logging.ERROR))
    first = listener.queue.get(False)
    assert first is not None
    assert first.getMessage() == "critical synthetic canary"


def test_capture_does_not_retain_unbounded_message_exception_or_extra_payload() -> None:
    handler, listener = logging_setup._build_capture_pipeline(logging.INFO, 20_000)
    record = _record("x" * 100_000)
    record.exc_text = "synthetic traceback: " + "x" * 100_000
    record.arbitrary_payload = "x" * 100_000
    handler.handle(record)
    pending = listener.queue.get(False)
    assert pending is not None
    assert len(pending.getMessage().encode()) <= 8192
    assert pending.exc_text is not None and len(pending.exc_text.encode()) <= 8192
    assert not hasattr(pending, "arbitrary_payload")


def test_rotating_ui_identities_cannot_exceed_a_global_record_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_700_000_001.0
    monkeypatch.setattr(log_admission, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(settings, "logs_ingest_per_minute", 10000)
    client = make_client()
    statuses = [
        client.post(
            "/api/logs",
            headers={"X-Client-ID": f"synthetic-owner-{index}"},
            json={"records": [{"message": "synthetic UI record"}] * 50},
        ).status_code
        for index in range(41)
    ]
    assert statuses[:40] == [200] * 40
    assert statuses[40] == 429
    now += 60
    assert (
        client.post(
            "/api/logs",
            headers={"X-Client-ID": "synthetic-owner-next-minute"},
            json={"records": [{"message": "synthetic UI record"}] * 50},
        ).status_code
        == 200
    )


def test_ui_retention_cannot_evict_server_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    isolated_db: str,
) -> None:
    monkeypatch.setattr(logs, "UI_LOG_MAX_ROWS", 3, raising=False)
    logs.append_log(
        NewLogRecord("ERROR", logging.ERROR, "app.server", "server canary"), db_path=isolated_db
    )
    response = make_client().post(
        "/api/logs",
        json={"records": [{"message": f"ui-{index}"} for index in range(10)]},
    )
    assert response.status_code == 200
    capture = logging_setup.configure_log_capture(max_rows=4)
    capture.stop()
    stored = logs.list_logs(db_path=isolated_db)
    assert "server canary" in [row["message"] for row in stored]
    assert sum(row["logger"] == "ui" or row["logger"].startswith("ui.") for row in stored) <= 3


def test_full_capture_can_stop_without_a_full_queue_sentinel_error() -> None:
    handler, listener = logging_setup._build_capture_pipeline(logging.INFO, 20_000)
    received: list[str] = []

    class Writer(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            received.append(record.getMessage())

    listener.handlers = (Writer(),)
    for index in range(1024):
        handler.handle(_record(f"synthetic-{index}"))
    handler.handle(_record("critical synthetic canary", logging.ERROR))
    listener.start()
    listener.stop()
    assert received[0] == "critical synthetic canary"
    assert listener.queue.stats()["queued_records"] == 0


def test_busy_capture_shutdown_discards_pending_records_after_a_bounded_wait() -> None:
    handler, listener = logging_setup._build_capture_pipeline(logging.INFO, 20_000)
    entered = threading.Event()
    release = threading.Event()

    class Writer(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            entered.set()
            assert release.wait(8)

    listener.handlers = (Writer(),)
    listener.start()
    thread = listener._thread
    handler.handle(_record("synthetic blocked write"))
    assert entered.wait(2)
    for index in range(16):
        handler.handle(_record(f"pending-{index}"))
    try:
        started = time.monotonic()
        listener.stop()
        assert time.monotonic() - started < 6
        assert listener.queue.stats()["queued_records"] == 0
        assert listener.queue.stats()["dropped_normal"] >= 16
    finally:
        release.set()
        assert thread is not None
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_capture_writer_remains_bound_to_its_original_database(
    monkeypatch: pytest.MonkeyPatch,
    isolated_db: str,
    tmp_path: Path,
) -> None:
    handler, listener = logging_setup._build_capture_pipeline(logging.INFO, 20_000)
    other = str(tmp_path / "other.db")
    monkeypatch.setenv("COSCIENTIST_DB_PATH", other)
    listener.start()
    handler.handle(_record("original database canary"))
    listener.stop()
    assert [row["message"] for row in logs.list_logs(db_path=isolated_db)] == [
        "original database canary"
    ]
    assert logs.list_logs(db_path=other) == []


def test_capture_critical_retention_is_separate_from_ordinary_traffic(isolated_db: str) -> None:
    logs.append_log(
        NewLogRecord("ERROR", logging.ERROR, "app.server", "critical canary"), db_path=isolated_db
    )
    for index in range(16):
        logs.append_log(
            NewLogRecord("INFO", logging.INFO, "app.server", f"ordinary-{index}"),
            db_path=isolated_db,
        )
    logs.prune_capture_logs(max_rows=4, db_path=isolated_db)
    stored = logs.list_logs(db_path=isolated_db)
    assert "critical canary" in [row["message"] for row in stored]
    assert sum(row["levelno"] < logging.WARNING for row in stored) <= 3


def test_global_ui_byte_admission_survives_a_fresh_process(
    monkeypatch: pytest.MonkeyPatch, isolated_db: str
) -> None:
    from co_scientist.platform import db
    from co_scientist.platform.db import log_admission

    minute = int(time.time() // 60)
    monkeypatch.setattr(time, "time", lambda: minute * 60 + 1)
    with db.transaction(isolated_db) as conn:
        assert log_admission.claim(conn, 1, log_admission.MAX_GLOBAL_BYTES)
    code = (
        "import sys; from co_scientist.platform import db; "
        "from co_scientist.platform.db import log_admission; "
        "log_admission.time.time=lambda: int(sys.argv[2])*60+1\n"
        "with db.transaction(sys.argv[1]) as conn:\n"
        " assert not log_admission.claim(conn, 1, 1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, isolated_db, str(minute)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
