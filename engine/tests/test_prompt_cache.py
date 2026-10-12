from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import json
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from co_scientist.core.exceptions import LLMContentFilteredError
from co_scientist.core.prompt_cache import CacheablePrompt
from co_scientist.platform.llm.profile import estimate_cost_usd
from co_scientist.platform.llm.request.azure import LUNA, response_request
from co_scientist.platform.llm.request.cache import (
    HAIKU,
    _azure_cache_key,
    _key_calls,
    apply_dispatch_cache_key,
    apply_prompt_cache,
)
from co_scientist.platform.llm.request.completion import _inject_schema_into_prompt
from co_scientist.platform.llm.request.response import (
    _extract_completion_content,
    extract_token_usage,
)
from co_scientist.platform.llm.request.transport import complete_request
from co_scientist.platform.llm.roles import scoped_call_policy
from co_scientist.platform.llm.telemetry import scoped_telemetry
from co_scientist.platform.telemetry.logging_setup import run_log_context


@pytest.fixture(autouse=True)
async def _fresh_sdk_logging_worker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    import litellm
    from litellm.litellm_core_utils import logging_worker

    monkeypatch.setattr(
        litellm,
        "model_cost",
        {key: value for key, value in litellm.model_cost.items() if "claude-haiku-5-5" not in key},
    )
    # The SDK callback queue is loop-bound; each pytest loop needs its own worker.
    worker = logging_worker.LoggingWorker()
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    yield
    await asyncio.sleep(0)
    await asyncio.wait_for(worker.flush(), 5)
    await worker.stop()


def _prompt() -> CacheablePrompt:
    run = "fixed instructions\n" + "shared evidence " * 1200
    item = "item A\n"
    return CacheablePrompt(run + item + "question?", run_end=len(run), item_end=len(run + item))


async def test_real_sdk_preserves_boundary_markers_thinking_and_cache_write_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "test-one",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "content": [
                    {"type": "thinking", "thinking": "private reasoning", "signature": "signed"},
                    {"type": "text", "text": "answer"},
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 800,
                    "cache_creation_input_tokens": 1100,
                },
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    prompt = _prompt()
    args: dict[str, Any] = {
        "model": HAIKU,
        "api_key": "fake",
        "drop_params": True,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning_effort": "none",
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
        "top_p": 0.5,
        "top_k": 3,
        "extra_body": {"temperature": 0.2, "top_p": 0.5, "top_k": 3},
    }
    with scoped_call_policy("claims", enable_thinking=False), scoped_telemetry("claims") as meter:
        response = await complete_request(args, HAIKU, byok=True, timeout_seconds=5)
    assert len(sent) == 1
    body = sent[0]
    assert "thinking" in body, body
    assert body["thinking"] == {"type": "disabled"}
    assert body["output_config"] == {"effort": "low"}
    assert not {"temperature", "top_p", "top_k"} & body.keys()
    assert _extract_completion_content(response, HAIKU) == "answer"
    blocks = body["messages"][0]["content"]
    assert "".join(b["text"] for b in blocks) == prompt
    assert [b["text"] for b in blocks[:2]] == [
        prompt[: prompt.run_end],
        prompt[prompt.run_end : prompt.item_end],
    ]
    assert [b["cache_control"] for b in blocks[:2]] == [{"type": "ephemeral", "ttl": "5m"}] * 2
    assert "cache_control" not in blocks[2]
    usage = extract_token_usage(response)
    assert (usage.prompt_tokens, usage.cached_prompt_tokens, usage.cache_write_tokens) == (
        2000,
        800,
        1100,
    )
    stats = meter.snapshot()[f"claims::{HAIKU}"]
    assert stats["cache_write_tokens"] == 1100
    assert stats["completion_tokens"] == 50
    assert stats["cost_usd"] == pytest.approx(
        (100 * 0.1 + 800 * 0.01 + 1100 * 0.125 + 50 * 0.5) / 1e6
    )


async def test_real_sdk_refusal_is_counted_once_and_never_parsed_as_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "test-refusal",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "stop_sequence": None,
                "stop_reason": "refusal" if len(sent) == 1 else "end_turn",
                "content": [{"type": "text", "text": "declined" if len(sent) == 1 else "answer"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    args: dict[str, Any] = {
        "model": HAIKU,
        "api_key": "fake",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": "goal"}],
    }
    with scoped_call_policy("claims"), scoped_telemetry("claims") as meter:
        refused = await complete_request(dict(args), HAIKU, byok=True, timeout_seconds=5)
        with pytest.raises(LLMContentFilteredError):
            _extract_completion_content(refused, HAIKU)
        accepted = await complete_request(dict(args), HAIKU, byok=True, timeout_seconds=5)
        assert _extract_completion_content(accepted, HAIKU) == "answer"
    stats = meter.snapshot()[f"claims::{HAIKU}"]
    assert len(sent) == stats["calls"] == 2
    assert stats["refusals"] == 1
    assert stats["completion_tokens"] == 100


def test_content_blocks_are_read_by_type_without_thinking_text() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=[
                        {"type": "thinking", "thinking": "private reasoning"},
                        {"type": "text", "text": "first"},
                        SimpleNamespace(type="text", text=" second"),
                    ]
                ),
            )
        ]
    )
    assert _extract_completion_content(response, HAIKU) == "first second"


@pytest.mark.parametrize("tools,role", [(True, "worker"), (False, "chat"), (False, "interview")])
def test_growing_transcripts_use_automatic_five_minute_cache(tools: bool, role: Any) -> None:
    args: dict[str, Any] = {"messages": [{"role": "user", "content": "hello"}]}
    if tools:
        args["tools"] = [{"type": "function"}]
    with scoped_call_policy(role):
        apply_prompt_cache(args, HAIKU)
    assert args["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


def test_schema_suffix_keeps_trusted_boundaries_and_other_models_are_unchanged() -> None:
    original = _prompt()
    rendered = _inject_schema_into_prompt(original, {"type": "object"})
    assert isinstance(rendered, CacheablePrompt)
    assert rendered[: rendered.item_end] == original[: original.item_end]
    request = {"messages": [{"role": "user", "content": rendered}]}
    apply_prompt_cache(request, LUNA)
    assert request["messages"][0]["content"] is rendered


@pytest.mark.parametrize("run_end,item_end", [(0, 2), (2, 1), (1, 100), (True, 2)])
def test_invalid_boundaries_refuse(run_end: int, item_end: int) -> None:
    with pytest.raises(ValueError):
        CacheablePrompt("abc", run_end=run_end, item_end=item_end)


def test_partition_keys_stay_under_fifteen_calls_in_a_rolling_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [59.0]
    monkeypatch.setattr("co_scientist.platform.llm.request.cache.time.monotonic", lambda: clock[0])
    _key_calls.clear()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        keys = list(pool.map(lambda _: _azure_cache_key("run", "claims"), range(31)))
    assert keys.count("run:claims") == 15
    assert keys.count("run:claims:1") == 15
    assert keys.count("run:claims:2") == 1
    clock[0] = 60.0
    assert _azure_cache_key("run", "claims") == "run:claims:2"
    clock[0] = 119.0
    assert _azure_cache_key("run", "claims") == "run:claims"
    assert _azure_cache_key("other", "claims") == "other:claims"
    assert _azure_cache_key("run", "ranking") == "run:ranking"


@pytest.mark.parametrize("model", [LUNA])
def test_azure_responses_carries_run_and_call_type_key(model: str) -> None:
    request = {
        "model": model,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": "hello"}],
    }
    _key_calls.clear()
    with run_log_context("run-one"), scoped_call_policy("claims"):
        apply_dispatch_cache_key(request, model)
        body = response_request(request, {model: "deployment"})
    assert body["prompt_cache_key"] == "run-one:claims"
    assert "prompt_cache_options" not in body


@pytest.mark.parametrize("tokens,rate", [(100000, 0.125), (100001, 0.625)])
def test_haiku_write_only_prefix_uses_its_published_long_context_rate(
    tokens: int, rate: float
) -> None:
    assert estimate_cost_usd(HAIKU, tokens, 0, 0, tokens) == pytest.approx(tokens * rate / 1e6)


def test_azure_write_count_reaches_general_usage_and_additive_cost() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=2000,
            completion_tokens=50,
            prompt_tokens_details=SimpleNamespace(cached_tokens=800, cache_write_tokens=1100),
        )
    )
    assert extract_token_usage(response).cache_write_tokens == 1100
    assert estimate_cost_usd(LUNA, 2000, 50, 800, 1100) == pytest.approx(
        (1200 * 0.1 + 800 * 0.01 + 1100 * 0.125 + 50 * 0.5) / 1e6
    )


def test_too_many_breakpoints_refuse_before_dispatch() -> None:
    from co_scientist.core.exceptions import ProviderAdmissionError

    request = {"messages": [{"role": "user", "content": _prompt()}] * 3}
    with pytest.raises(ProviderAdmissionError, match="Too many"):
        apply_prompt_cache(request, HAIKU)


async def test_real_sdk_preserves_top_level_automatic_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "test-chat",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "content": [{"type": "text", "text": "answer"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    args: dict[str, Any] = {
        "model": HAIKU,
        "api_key": "fake",
        "drop_params": True,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": "chat"}],
    }
    with scoped_call_policy("chat"):
        await complete_request(args, HAIKU, byok=True, timeout_seconds=5)
    assert sent[0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert sent[0]["thinking"] == {"type": "adaptive"}
    assert sent[0]["output_config"] == {"effort": "low"}


def test_private_span_keeps_only_numeric_cache_counts() -> None:
    from opentelemetry.sdk.trace import ReadableSpan

    from co_scientist.platform.telemetry.tracing import _private_span

    span = ReadableSpan(
        name="private prompt",
        attributes={
            "co_scientist.llm.call_type": 27,
            "gen_ai.usage.input_tokens": 2000,
            "co_scientist.llm.cached_prompt_tokens": 800,
            "co_scientist.llm.cache_write_tokens": 1100,
            "co_scientist.llm.refusal": 1,
            "prompt_cache_key": "secret-run:claims",
            "prompt": "confidential text",
        },
    )
    projected = _private_span(span)
    assert dict(projected.attributes or {}) == {
        "co_scientist.llm.call_type": 27,
        "gen_ai.usage.input_tokens": 2000,
        "co_scientist.llm.cached_prompt_tokens": 800,
        "co_scientist.llm.cache_write_tokens": 1100,
        "co_scientist.llm.refusal": 1,
    }


@pytest.mark.parametrize("failed", [False, True])
async def test_stream_refusal_survives_a_later_usage_only_chunk(failed: bool) -> None:
    from co_scientist.platform.llm.request.backend import using_backend

    async def chunks() -> AsyncIterator[Any]:
        yield SimpleNamespace(choices=[SimpleNamespace(finish_reason="content_filter")], usage=None)
        yield SimpleNamespace(
            choices=[], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50)
        )
        if failed:
            raise RuntimeError("stream failed")

    class Fake:
        async def complete(self, **kwargs: Any) -> Any:
            return chunks()

        def supports_json_schema(self, model_name: str) -> bool:
            return True

    with using_backend(Fake()), scoped_call_policy("chat"), scoped_telemetry("chat") as meter:
        stream = await complete_request(
            {"model": HAIKU, "stream": True, "messages": [], "max_tokens": 1000},
            HAIKU,
            byok=True,
            timeout_seconds=5,
        )

        async def consume() -> None:
            async for _ in stream:
                pass

        if failed:
            with pytest.raises(RuntimeError, match="stream failed"):
                await consume()
        else:
            await consume()
    stats = meter.snapshot()[f"chat::{HAIKU}"]
    assert stats["calls"] == stats["refusals"] == 1
    assert (stats["prompt_tokens"], stats["completion_tokens"]) == ((0, 0) if failed else (100, 50))


async def test_real_sdk_keeps_signed_adaptive_thinking_across_tool_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.platform.llm.tools.transcript import _message_to_history_dict

    sent = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(json.loads(request.content))
        content: list[dict[str, Any]] = [{"type": "text", "text": "answer"}]
        if len(sent) == 1:
            content = [
                {"type": "thinking", "thinking": "private reasoning", "signature": "signed"},
                {"type": "tool_use", "id": "tool-one", "name": "lookup", "input": {}},
            ]
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "test-tool",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "stop_reason": "tool_use" if len(sent) == 1 else "end_turn",
                "stop_sequence": None,
                "content": content,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    args: dict[str, Any] = {
        "model": HAIKU,
        "api_key": "fake",
        "drop_params": True,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": "chat"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "lookup",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    first = await complete_request(args, HAIKU, byok=True, timeout_seconds=5)
    args["messages"].extend(
        [
            _message_to_history_dict(first.choices[0].message),
            {"role": "tool", "tool_call_id": "tool-one", "content": "result"},
        ]
    )
    await complete_request(args, HAIKU, byok=True, timeout_seconds=5)
    assert len(sent) == 2
    for request in sent:
        assert request["thinking"] == {"type": "adaptive"}
        assert request["output_config"] == {"effort": "low"}
        assert request["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    thinking = sent[1]["messages"][1]["content"][0]
    assert thinking == {"type": "thinking", "thinking": "private reasoning", "signature": "signed"}


def test_cache_key_state_has_a_fixed_memory_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("co_scientist.platform.llm.request.cache._KEYS_LIMIT", 2)
    _key_calls.clear()
    for run in ("first", "second", "third"):
        _azure_cache_key(run, "claims")
    assert list(_key_calls) == ["second:claims", "third:claims"]
    for _ in range(4000):
        _azure_cache_key("third", "claims")
    assert len(_key_calls["third:claims"]) == 256
    assert all(len(calls) <= 15 for calls in _key_calls["third:claims"].values())


async def test_real_qa_sdk_stream_replays_signed_thinking_without_extra_calls(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from co_scientist.core.byok_scope import ByokCredential, scoped_byok
    from co_scientist.domains.chat import qa
    from co_scientist.domains.chat.qa.manifest import SEARCH_IDEAS_TOOL

    sent = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(json.loads(request.content))
        tool_turn = len(sent) == 1
        events: list[dict[str, Any]] = [
            {
                "type": "message_start",
                "message": {
                    "id": "stream-test",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-5-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 1100 if tool_turn else 0,
                        "cache_read_input_tokens": 800 if tool_turn else 0,
                    },
                },
            }
        ]
        if tool_turn:
            events.extend(
                [
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": "private reasoning"},
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "signature_delta", "signature": "signed"},
                    },
                    {"type": "content_block_stop", "index": 0},
                    {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {
                            "type": "tool_use",
                            "id": "tool-one",
                            "name": SEARCH_IDEAS_TOOL,
                            "input": {},
                        },
                    },
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "input_json_delta", "partial_json": "{}"},
                    },
                    {"type": "content_block_stop", "index": 1},
                ]
            )
        else:
            events.extend(
                [
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "answer"},
                    },
                    {"type": "content_block_stop", "index": 0},
                ]
            )
        events.extend(
            [
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "tool_use" if tool_turn else "end_turn",
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": 50},
                },
                {"type": "message_stop"},
            ]
        )
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(
            200, request=request, headers={"content-type": "text/event-stream"}, content=body
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with (
        scoped_byok(ByokCredential("anthropic", "fake", HAIKU)),
        caplog.at_level(logging.INFO, logger="co_scientist.platform.llm.llm_scope"),
    ):
        frames = [
            frame
            async for frame in qa.stream_llm_deltas(
                HAIKU,
                "fixed instructions",
                "question?",
                [{"id": "idea-one", "text": "hypothesis"}],
            )
        ]
    assert frames[-1] == ("chunk", "answer")
    assert len(sent) == 2
    for request in sent:
        assert request["thinking"] == {"type": "adaptive"}
        assert request["output_config"] == {"effort": "low"}
        assert request["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assistant = next(message for message in sent[1]["messages"] if message["role"] == "assistant")
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "private reasoning",
        "signature": "signed",
    }

    receipt = next(
        record.getMessage()
        for record in caplog.records
        if "App completion usage surface=qa" in record.getMessage()
    )
    stats = ast.literal_eval(receipt.split("usage=", 1)[1])[f"app.qa::{HAIKU}"]
    assert stats["cache_write_tokens"] == 1100
    assert stats["cached_prompt_tokens"] == 800
    assert stats["prompt_tokens"] == 2100
    assert stats["cost_usd"] == pytest.approx(
        (200 * 0.1 + 800 * 0.01 + 1100 * 0.125 + 100 * 0.5) / 1e6
    )
