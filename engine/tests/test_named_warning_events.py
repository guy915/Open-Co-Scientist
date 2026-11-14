from __future__ import annotations

import logging

import pytest

from co_scientist.platform.telemetry.logging_setup import diagnostic_projection

_NAMED = (
    "interview.state_missing",
    "interview.question_unusable",
    "schema_retry",
    "claim_gate.unverified",
    "report.redacted",
    "report.withheld",
)


@pytest.mark.parametrize("event", _NAMED)
def test_a_named_research_warning_keeps_its_name_but_not_its_text(event: str) -> None:
    record = logging.LogRecord(
        "co_scientist.anywhere", logging.WARNING, __file__, 7, "private %s %d", ("goal", 3), None
    )
    record.diagnostic_event = event

    message = diagnostic_projection(record).getMessage()

    assert message == f"Research diagnostic event={event} at line 7; numeric values=[3]"
    assert "goal" not in message


def test_an_unknown_name_still_reads_other() -> None:
    record = logging.LogRecord("co_scientist.x", logging.WARNING, __file__, 1, "t", (), None)
    record.diagnostic_event = "free text from a goal"

    assert "event=other" in diagnostic_projection(record).getMessage()


def test_a_named_event_exports_scrubbed_short_fields() -> None:
    record = logging.LogRecord("co_scientist.x", logging.WARNING, __file__, 3, "private", (), None)
    record.diagnostic_event = "llm.provider_failure"
    record.diagnostic_fields = {
        "status": 429,
        "provider_message": "Bearer abc.def for ops@example.org key sk-or-v1-0123456789 "
        + "x" * 300,
        "Bad Name": "dropped",
        "nested": {"goal": "private"},
    }

    message = diagnostic_projection(record).getMessage()

    assert "status=429" in message
    assert "provider_message=" in message and "…" in message
    for leaked in ("abc.def", "ops@example.org", "sk-or-v1", "Bad Name", "private"):
        assert leaked not in message


def test_an_unnamed_event_exports_no_fields() -> None:
    record = logging.LogRecord("co_scientist.x", logging.WARNING, __file__, 3, "t", (), None)
    record.diagnostic_event = "not.named"
    record.diagnostic_fields = {"provider_message": "goal text"}

    assert "goal text" not in diagnostic_projection(record).getMessage()
