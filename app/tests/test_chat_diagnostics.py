from __future__ import annotations

import logging

import pytest
from co_scientist.platform.db import logs
from co_scientist.platform.db.log_capture import configure_log_capture, shutdown_log_capture
from co_scientist.platform.db.logs import LogFilters
from co_scientist.platform.telemetry.diagnostic_events import log_chat_turn


def test_chat_metadata_is_owned_and_excludes_text(
    isolated_db: str, caplog: pytest.LogCaptureFixture
) -> None:
    configure_log_capture()
    try:
        with caplog.at_level(logging.INFO, logger="app.chat_turn"):
            log_chat_turn("user", "private user secret", owner="alice")
            log_chat_turn(
                "agent",
                "private answer secret",
                owner="alice",
                duration_seconds=1.25,
            )
    finally:
        shutdown_log_capture()
    owned = logs.list_logs(filters=LogFilters(scope_client_id="alice"))
    assert len(owned) == 2
    assert logs.list_logs(filters=LogFilters(scope_client_id="bob")) == []
    assert "role=user chars=19" in owned[0]["message"]
    assert "role=agent" in owned[1]["message"] and "duration_seconds=1.250" in owned[1]["message"]
    assert "private" not in str(owned)
