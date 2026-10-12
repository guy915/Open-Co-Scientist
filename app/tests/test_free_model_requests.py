import asyncio
import os
from types import SimpleNamespace
from typing import Any

import co_scientist.domains.chat.qa as qa_stream
import pytest
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.domains.access import credentials
from co_scientist.domains.chat import goal_text, run_start_announcement
from co_scientist.domains.chat.interviews import model as interviews_model
from co_scientist.platform.llm import scoped_zero_cost_admission

from tests._llm_fake_backend import (
    completion_response,
    install_completion_backend,
)
from tests._store_helpers import seed_run

MODEL = "openrouter/free/chat:free"
KINDS = ["interview", "qa", "announcement", "title", "restatement", "probe"]


async def _stream_call(kind: str, model: str) -> Any:
    if kind == "interview":
        return await interviews_model._stream_interview_content(
            {
                "turns": [{"role": "user", "content": "Public research"}],
                "fields": {},
            },
            interviews_model.TurnSinks(),
        )
    if kind == "qa":
        return [item async for item in qa_stream.stream_llm_deltas(model, "system", "question", [])]
    run = seed_run("Public research", profile="express")
    return [item async for item in run_start_announcement._stream_model_fragments(run)]


async def _invoke(kind: str, model: str) -> Any:
    if kind == "title":
        return await goal_text._request_completion("Public research", goal_text._TITLE)
    if kind == "restatement":
        return await goal_text._request_completion("Public research", goal_text._RESTATEMENT)
    if kind == "probe":
        return await credentials.validate_byok_credential(
            byok_scope.ByokCredential("openrouter", "test-user-key", model)
        )
    return await _stream_call(kind, model)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch, reachable_provider: None) -> list[dict[str, Any]]:
    from co_scientist.platform.llm.admission import free_policy as free_catalog

    monkeypatch.setattr(
        free_catalog,
        "_reader",
        free_catalog.CatalogReader(
            lambda: {
                "free/chat:free": {
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                    },
                }
            }
        ),
    )
    monkeypatch.setattr(settings, "chat_model_name", MODEL)
    calls: list[dict[str, Any]] = []

    async def chunks() -> Any:
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="answer", reasoning_content="reasoning")
                )
            ]
        )

    async def complete(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if kwargs.get("stream"):
            return chunks()
        return completion_response("answer")

    install_completion_backend(monkeypatch, complete)
    return calls


@pytest.mark.parametrize("kind", KINDS)
async def test_free_only_mode_blocks_paid_app_calls(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
    kind: str,
) -> None:
    monkeypatch.setenv("COSCIENTIST_REQUIRE_FREE_MODELS", "1")
    monkeypatch.setattr(settings, "chat_model_name", "openrouter/free/paid")
    with pytest.raises(Exception, match="zero-cost"):
        await _invoke(kind, "openrouter/free/paid")
    assert captured == []


@pytest.mark.parametrize("kind", KINDS)
async def test_free_only_mode_free_requests_keep_streams_and_zero_caps(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
    kind: str,
) -> None:
    monkeypatch.setenv("COSCIENTIST_REQUIRE_FREE_MODELS", "1")
    result = await _invoke(kind, MODEL)
    assert len(captured) == 1
    request = captured[0]
    assert request["api_base"] == "https://openrouter.ai/api/v1"
    assert request["extra_body"]["provider"]["max_price"] == {
        "prompt": 0,
        "completion": 0,
        "request": 0,
    }
    if kind in {"qa", "announcement"}:
        assert ("reasoning", "reasoning") in result
        assert ("chunk", "answer") in result
    if kind == "interview":
        assert result[0] == "answer"
    assert request.get("stream", False) == (kind in {"interview", "qa", "announcement"})


async def test_concurrent_zero_cost_scope_and_standard_byok_stay_isolated(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
) -> None:
    monkeypatch.delenv("COSCIENTIST_REQUIRE_FREE_MODELS", raising=False)
    credential = byok_scope.ByokCredential("openrouter", "test-user-key", "openrouter/free/paid")

    async def zero_cost_call() -> None:
        with (
            scoped_zero_cost_admission(True),
            byok_scope.scoped_byok(credential),
            pytest.raises(Exception, match="zero-cost"),
        ):
            await _invoke("title", credential.model)

    async def standard_call() -> None:
        with (
            byok_scope.scoped_byok(credential),
        ):
            await _invoke("title", credential.model)

    await asyncio.gather(zero_cost_call(), standard_call())

    assert len(captured) == 1
    assert captured[0]["api_key"] == credential.api_key
    assert captured[0]["model"] == credential.model
    assert os.getenv("COSCIENTIST_REQUIRE_FREE_MODELS") is None


async def test_free_qa_tool_continuation_keeps_admission(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:

    from tests.test_qa_ideas import (
        _chunk,
        _idea,
        _scripted_litellm,
        _search_call_chunk,
    )

    monkeypatch.setenv("COSCIENTIST_REQUIRE_FREE_MODELS", "1")
    fake = _scripted_litellm([[_search_call_chunk()], [_chunk("answer")]])
    install_completion_backend(monkeypatch, fake.acompletion)
    result = [
        item
        async for item in qa_stream.stream_llm_deltas(
            MODEL, "system", "question", [_idea("Lipid repair")]
        )
    ]
    assert result == [("chunk", "answer")]
    assert len(fake.sent) == 2
    assert fake.sent[1]["messages"][-1]["role"] == "tool"
    assert "Lipid repair" in fake.sent[1]["messages"][-1]["content"]
    for request in fake.sent:
        assert request["extra_body"]["provider"]["max_price"] == {
            "prompt": 0,
            "completion": 0,
            "request": 0,
        }


async def test_interview_reasoning_retry_rechecks_admission(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    from co_scientist.core.exceptions import FreeModelEligibilityError
    from co_scientist.platform.llm.admission import free_policy as free_catalog

    monkeypatch.setenv("COSCIENTIST_REQUIRE_FREE_MODELS", "1")

    async def complete(**kwargs: Any) -> Any:
        captured.append(kwargs)

        async def chunks() -> Any:
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, reasoning_content="thinking only")
                    )
                ]
            )
            monkeypatch.setattr(free_catalog, "_reader", free_catalog.CatalogReader(lambda: {}))

        return chunks()

    install_completion_backend(monkeypatch, complete)
    with pytest.raises(FreeModelEligibilityError):
        await _invoke("interview", MODEL)
    assert len(captured) == 1


async def test_unscoped_flag_cannot_bypass_offline_guard(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    from co_scientist.platform.llm import llm_request
    from co_scientist.platform.llm.offline_guard import OfflineModeError

    monkeypatch.delenv("COSCIENTIST_REQUIRE_FREE_MODELS", raising=False)
    monkeypatch.setenv("COSCIENTIST_TEST_DOUBLE", "deterministic")
    with pytest.raises(OfflineModeError):
        await llm_request.acompletion(byok=True, model=MODEL, messages=[])
    assert captured == []
