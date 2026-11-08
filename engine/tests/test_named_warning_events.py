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
