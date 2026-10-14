from __future__ import annotations

import logging
from typing import Any

import pytest

from co_scientist.platform.llm import (
    CompletionSpec,
    call_llm,
    call_llm_json,
    scoped_telemetry,
)
from tests._llm_fake import (
    make_completion,
    make_message,
    make_usage,
    patch_acompletion,
    scripted_backend,
)

_MODEL = "deepseek/deepseek-v4-flash"
_INT_SCHEMA: dict[str, Any] = {
    "name": "ranking_judgment",
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
}

# LiteLLM parse failures can append the entire original completion.
_HUGE_PROVIDER_ERROR = (
    "litellm.APIError: APIError: DeepseekException - Unable to get json "
    "response - Unterminated string starting at: line 1 column 184 "
    "(char 183), Original Response: " + ("{payload}" * 900)
)


def _answerless() -> Any:
    return make_completion(
        make_message(None),
        usage=make_usage(3938, 523, reasoning_tokens=523),
        finish_reason="stop",
    )


async def test_a_recovered_call_logs_one_failure_naming_what_was_sent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Layered duplicate records made a recovered run look like dozens of
    errors; the thinking floor can exceed the caller budget, and many call
    sites share a budget, so the record names both."""
    scripted_backend(
        monkeypatch,
        [_answerless(), make_completion(make_message('{"a":1}'))],
        repeat_last=True,
    )

    with caplog.at_level(logging.DEBUG, logger="co_scientist"):
        result = await call_llm_json(
            "a prompt",
            CompletionSpec(model_name=_MODEL, max_tokens=8000, json_schema=_INT_SCHEMA),
            max_attempts=5,
        )

    assert result == {"a": 1}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    failures = [r for r in caplog.records if "LLM call failed" in r.message]
    assert len(failures) == 1
    assert failures[0].name == "co_scientist.platform.llm.attempts.retry"
    message = failures[0].getMessage()
    assert "max_tokens 18000" in message
    assert "asked for 8000" in message
    assert "ranking_judgment" in message
    layers = {"co_scientist.platform.llm", "co_scientist.platform.llm.call"}
    assert not [r for r in caplog.records if r.name in layers and r.levelno >= logging.WARNING]
    assert any(
        r.levelno == logging.INFO and "retrying llm call" in r.getMessage() for r in caplog.records
    )


async def test_a_provider_error_is_logged_at_a_bounded_length(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scripted_backend(monkeypatch, [RuntimeError(_HUGE_PROVIDER_ERROR)], repeat_last=True)

    with (
        caplog.at_level(logging.DEBUG, logger="co_scientist"),
        pytest.raises(RuntimeError),
    ):
        await call_llm_json(
            "a prompt",
            CompletionSpec(model_name=_MODEL, json_schema=_INT_SCHEMA),
            max_attempts=1,
        )

    assert any("Unterminated string" in r.getMessage() for r in caplog.records)
    assert all(len(r.getMessage()) < 1000 for r in caplog.records)


async def test_known_zero_estimate_is_distinct_from_missing_cost_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = make_completion(make_message("ok"), make_usage(0, 0))
    response.model = "minimax/minimax-m3:free"
    patch_acompletion(monkeypatch, [response], [])
    with scoped_telemetry("probe") as telemetry:
        await call_llm(
            "probe",
            CompletionSpec("openrouter/probe"),
        )
    entry = telemetry.snapshot()["probe::openrouter/minimax/minimax-m3:free"]
    assert entry["cost_usd"] == 0
    assert entry["priced_usage_calls"] == 1
    assert entry["reported_usage_calls"] == 1
