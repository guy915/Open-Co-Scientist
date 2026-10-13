from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from co_scientist.api.runs.models import CreateRunRequest, _build_create_run_config
from co_scientist.core.exceptions import ResponseParseError
from co_scientist.core.run_modes import RUN_TIER_DEFAULTS, planning_limits, resolved_run_config
from co_scientist.orchestration.repository.events import log_event_stage
from co_scientist.platform.db import logs
from co_scientist.platform.db.log_capture import configure_log_capture, shutdown_log_capture
from co_scientist.platform.llm.structured.validate import (
    _log_json_parse_failure_diagnostics,
    parse_tool_loop_json,
)
from co_scientist.platform.telemetry.logging_setup import JsonFormatter, configure_logging
from co_scientist.science.supervisor.supervisor import _log_key_areas
from pydantic import ValidationError

from ._client import make_client


@pytest.mark.parametrize("tier", RUN_TIER_DEFAULTS)
@pytest.mark.parametrize(
    "field", ["initial_hypotheses_count", "max_iterations", "evolution_max_count", "k_factor"]
)
def test_run_planning_accepts_tier_maximum_and_rejects_excess(tier: str, field: str) -> None:
    maximum = planning_limits(tier)[field]
    req = CreateRunRequest(research_goal="A research goal", tier=tier, **{field: maximum})
    config, _, _ = _build_create_run_config(req)
    assert config[field] == maximum
    with pytest.raises(ValidationError):
        CreateRunRequest(research_goal="A research goal", tier=tier, **{field: maximum + 1})
    with pytest.raises(ValueError):
        resolved_run_config({"tier": tier, field: maximum + 1})


@pytest.mark.parametrize(
    "field", ["initial_hypotheses_count", "max_iterations", "evolution_max_count", "k_factor"]
)
def test_oversized_planning_is_rejected_before_run_creation(isolated_db: str, field: str) -> None:
    with make_client() as client:
        response = client.post("/api/runs", json={"research_goal": "A goal", field: 10**12})
    assert response.status_code == 422


def test_low_legacy_overrides_still_preserve_tier_baselines() -> None:
    assert resolved_run_config({"max_iterations": 0})["max_iterations"] == 2


@pytest.mark.parametrize(
    "namespace",
    [
        "co_scientist.science.supervisor.supervisor",
        "co_scientist.platform.llm.structured.validate",
        "co_scientist.platform.retrieval.research.loop",
        "co_scientist.domains.chat.interviews",
        "co_scientist.domains.documents.ingest",
        "app.run_stage",
    ],
)
def test_scientific_diagnostics_project_prose_and_errors(namespace: str) -> None:
    canary = "private-goal-hypothesis-question-chat-document-canary"
    try:
        raise ValueError(canary)
    except ValueError:
        import sys

        record = logging.LogRecord(
            namespace, logging.ERROR, __file__, 90, canary, (), sys.exc_info()
        )
    payload = json.loads(JsonFormatter().format(record))
    assert canary not in json.dumps(payload)
    assert payload["logger"] == namespace
    assert payload["exc_info"] == "ValueError"


def test_parse_failure_and_research_areas_never_reach_stdout_or_persistence(
    isolated_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "PRIVATE-RESEARCH-CANARY"
    handler = configure_logging()
    configure_log_capture()
    try:
        _log_key_areas([canary])
        _log_json_parse_failure_diagnostics(canary)
        log_event_stage("research-run", "report", {"research_goal": canary})
        with pytest.raises(ResponseParseError):
            parse_tool_loop_json(canary, "drafts", "Draft phase")
    finally:
        shutdown_log_capture()
        logging.getLogger().removeHandler(handler)
    output = capsys.readouterr().out
    rows: list[dict[str, Any]] = logs.list_logs(db_path=isolated_db)
    assert rows
    assert canary not in output
    assert canary not in json.dumps(rows)
    assert "Research diagnostic" in output
    assert "event=report" in output


def test_sdk_stream_handlers_cannot_bypass_privacy_projection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    canary = "PRIVATE-SDK-DIAGNOSTIC-CANARY"
    sdk_logger = logging.getLogger("LiteLLM")
    sdk_logger.addHandler(logging.StreamHandler(sys.stderr))
    handler = configure_logging()
    try:
        sdk_logger.warning(canary)
    finally:
        logging.getLogger().removeHandler(handler)
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
    assert "Research diagnostic" in captured.out
