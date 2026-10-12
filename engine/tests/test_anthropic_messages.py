from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest

from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import connect
from co_scientist.platform.db.anthropic_credit import AnthropicPromptTooLongError
from co_scientist.platform.llm.admission.anthropic import credit_config
from co_scientist.platform.llm.admission.service import scoped_client
from co_scientist.platform.llm.request.anthropic import (
    AnthropicSlotUnavailableError,
    count_request,
    is_credit_error,
)
from co_scientist.platform.llm.request.cache import HAIKU
from co_scientist.platform.llm.request.transport import complete_request
from co_scientist.platform.llm.roles import CallRole, scoped_call_policy


@pytest.fixture
def credit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    path = str(tmp_path / "credit.db")
    monkeypatch.setenv("COSCIENTIST_DB_PATH", path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setenv("ANTHROPIC_MONTHLY_CREDIT_USD", "100")
    monkeypatch.setenv("ANTHROPIC_BILLING_RESET_DAY", "7")
    monkeypatch.setenv("LLM_ENABLED", "true")
    return path


def _request() -> dict[str, Any]:
    return {"model": HAIKU, "messages": [{"role": "user", "content": "goal"}], "max_tokens": 2000}


@pytest.mark.parametrize("count,accepted", [(100_000, True), (100_001, False)])
async def test_own_token_count_checks_cap_before_any_message_is_sent(
    credit: str, monkeypatch: pytest.MonkeyPatch, count: int, accepted: bool
) -> None:
    sent: list[httpx.Request] = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(request)
        # A different writer can acquire immediately during both HTTP calls.
        with connect(credit) as conn:
            conn.execute("PRAGMA busy_timeout=0")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, request=request, json={"input_tokens": count})
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "test-message",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "content": [{"type": "text", "text": "answer"}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 800,
                    "cache_creation_input_tokens": 1100,
                },
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with scoped_client("owner", db_path=credit), scoped_call_policy("relevance"):
        if accepted:
            await complete_request(_request(), HAIKU, byok=False, timeout_seconds=5)
        else:
            with pytest.raises(AnthropicPromptTooLongError):
                await complete_request(_request(), HAIKU, byok=False, timeout_seconds=5)
    assert all(request.url.host == "api.anthropic.com" for request in sent)
    assert sum(request.url.path.endswith("/messages") for request in sent) == int(accepted)
    counted = json.loads(sent[0].content)
    assert counted["model"] == "claude-haiku-5-5"
    assert counted["thinking"] == {"type": "adaptive"}
    if accepted:
        message = json.loads(sent[-1].content)
        assert message["max_tokens"] == 2000
        assert message["thinking"] == {"type": "adaptive"}
        assert message["output_config"] == {"effort": "low"}
        assert not any(key in message for key in ("temperature", "top_p", "top_k"))
    with connect(credit) as conn:
        rows = conn.execute("SELECT charged_microusd FROM anthropic_credit").fetchall()
        assert [row[0] for row in rows] == ([181] if accepted else [])


async def test_95_percent_stops_before_even_the_free_count_endpoint(
    credit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.llm.admission.service import reserve_physical

    with scoped_client("owner", db_path=credit):
        reserve_physical(_request())
    config = credit_config()
    with connect(credit) as conn:
        conn.execute(
            "UPDATE anthropic_credit SET charged_microusd=?,reserved_microusd=?",
            (config.limit, config.limit),
        )

    async def send(*args: Any, **kwargs: Any) -> httpx.Response:
        raise AssertionError("exhausted slot must not send any HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with scoped_client("owner", db_path=credit), pytest.raises(ProviderAdmissionError):
        await complete_request(_request(), HAIKU, byok=False, timeout_seconds=5)


def test_prefill_and_other_models_cannot_enter_the_credit_slot() -> None:
    with pytest.raises(AnthropicSlotUnavailableError):
        count_request({**_request(), "messages": [{"role": "assistant", "content": "prefix"}]})
    with pytest.raises(AnthropicSlotUnavailableError):
        count_request({**_request(), "model": "anthropic/claude-opus-5-5"})
    with pytest.raises(AnthropicSlotUnavailableError):
        count_request({**_request(), "extra_body": {"model": "claude-opus-5-5"}})


def test_credit_error_classification_uses_transport_errors_only() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API",
            }
        },
    )
    error = httpx.HTTPStatusError("bad request", request=request, response=response)
    assert is_credit_error(error)
    assert not is_credit_error(
        ValueError("Your credit balance is too low to access the Anthropic API")
    )


async def test_real_sdk_exhaustion_has_no_retry_and_persists_operator_cooldown(
    credit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(request)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, request=request, json={"input_tokens": 20})
        return httpx.Response(
            400,
            request=request,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low to access the Anthropic API",
                },
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with scoped_client("owner", db_path=credit):
        with pytest.raises(AnthropicSlotUnavailableError):
            await complete_request(_request(), HAIKU, byok=False, timeout_seconds=5)
        with pytest.raises(ProviderAdmissionError):
            await complete_request(_request(), HAIKU, byok=False, timeout_seconds=5)
    assert len(sent) == 2
    with connect(credit) as conn:
        row = conn.execute(
            "SELECT disabled_until FROM anthropic_credit_state WHERE slot='operator'"
        ).fetchone()
        assert row[0] == credit_config().cycle_end
        assert conn.execute("SELECT settled FROM anthropic_credit").fetchone()[0] == 0


async def test_caller_billing_error_does_not_disable_operator_credit(
    credit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            402, request=request, json={"error": {"type": "billing_error", "message": "empty"}}
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with scoped_client("owner", db_path=credit), pytest.raises(openai.APIError):
        await complete_request(
            {**_request(), "api_key": "caller-key"}, HAIKU, byok=True, timeout_seconds=5
        )
    assert len(sent) == 1
    assert sent[0].headers["x-api-key"] == "caller-key"
    assert sent[0].url.path.endswith("/messages")
    with connect(credit) as conn:
        assert conn.execute("SELECT COUNT(*) FROM anthropic_credit_state").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM anthropic_credit").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("role", "cap", "thinking", "mode"),
    [
        ("claims", 8192, False, "disabled"),
        ("relevance", 8192, True, "adaptive"),
        ("goal_text", 8192, True, "disabled"),
        ("generation", 16384, True, "adaptive"),
        ("overview", 32768, True, "adaptive"),
    ],
)
def test_haiku_reasons_where_luna_does_and_always_at_low_effort(
    role: CallRole, cap: int, thinking: bool, mode: str
) -> None:
    from co_scientist.platform.llm.request.thinking import apply_provider_constraints

    request = {**_request(), "max_tokens": 128000, "temperature": 0.4, "top_p": 0.5}
    with scoped_call_policy(role, enable_thinking=thinking):
        apply_provider_constraints(request, HAIKU)
    assert request["max_tokens"] == cap
    assert request["thinking"] == {"type": mode}
    assert request["output_config"] == {"effort": "low"}
    assert "temperature" not in request and "top_p" not in request
