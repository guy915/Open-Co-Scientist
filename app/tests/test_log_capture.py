from __future__ import annotations

import logging
import logging.handlers

import pytest
from co_scientist.platform.db import logs
from co_scientist.platform.db.log_capture import configure_log_capture, shutdown_log_capture
from co_scientist.platform.telemetry.logging_setup import run_log_context


@pytest.fixture(autouse=True)
def _teardown_capture() -> object:
    yield
    shutdown_log_capture()


def _flush() -> None:
    shutdown_log_capture()


def _test_logger() -> logging.Logger:
    # Set logger levels explicitly because the root logger may remain at
    # WARNING.
    test_logger = logging.getLogger("app.capture_test")
    test_logger.setLevel(logging.INFO)
    return test_logger


def test_capture_persists_records_with_run_id_and_exception_text(
    isolated_db: str,
) -> None:
    configure_log_capture(level=logging.INFO)
    configure_log_capture(level=logging.INFO)
    test_logger = _test_logger()
    test_logger.info("plain record %d", 7)
    with run_log_context("run-42"):
        test_logger.warning("scoped record")
    try:
        raise ValueError("kaboom")
    except ValueError:
        test_logger.exception("operation failed")
    _flush()
    rows = logs.list_logs(db_path=isolated_db)
    assert len(rows) == 3
    by_message = {row["message"]: row for row in rows}
    assert "ValueError: kaboom" in by_message["operation failed"]["exc_text"]
    assert by_message["plain record 7"]["run_id"] is None
    assert by_message["plain record 7"]["level"] == "INFO"
    assert by_message["plain record 7"]["logger"] == "app.capture_test"
    assert by_message["scoped record"]["run_id"] == "run-42"


def test_store_failure_does_not_break_logging(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(**kwargs: object) -> int:
        raise RuntimeError("db exploded")

    monkeypatch.setattr("co_scientist.platform.db.log_capture.store.append_log", boom)
    configure_log_capture()
    test_logger = _test_logger()
    test_logger.info("first")
    test_logger.info("second")
    _flush()
    err = capsys.readouterr().err
    assert err.count("log capture") == 1
