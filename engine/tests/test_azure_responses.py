from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import openai
import pytest

from co_scientist.core.exceptions import LLMTimeoutError, ProviderAdmissionError
from co_scientist.domains.safety.semantic import _SEMANTIC_DECISION_SCHEMA
from co_scientist.platform.db import connect, transaction
from co_scientist.platform.db.admission import ProviderReservation
from co_scientist.platform.db.spend import SpendReservation
from co_scientist.platform.llm.admission.spend import azure_dispatch_permit
from co_scientist.platform.llm.request.azure import (
    LUNA,
    AzureResponsesBackend,
    Completion,
    ResponsesStream,
    normalize_response,
    normalize_usage,
    response_input,
    response_request,
)
from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
from co_scientist.platform.llm.request.completion import _apply_schema_response_format
from co_scientist.platform.llm.request.transport import complete_request
from co_scientist.platform.llm.roles import scoped_call_policy
from co_scientist.platform.llm.tools.transcript import _message_to_history_dict
from tests._azure_ledger import record_azure_allowance

DEPLOYMENTS = {LUNA: "luna-deployment"}


def _response(**changes: Any) -> dict[str, Any]:
    return {
        "id": "response-one",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "deployment",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 90,
            "output_tokens": 30,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens_details": {"reasoning_tokens": 20},
        },
        **changes,
    }


def _request(**changes: Any) -> dict[str, Any]:
    return {
        "model": LUNA,
        "messages": [{"role": "user", "content": "answer"}],
        "max_tokens": 18000,
        **changes,
    }


def _permit(model: str = LUNA) -> Any:
    # Wire-format tests call the backend directly, outside the funded gateway.
    money = SpendReservation(model, "worker", 0, 1, float("inf"), 10**9, 10**9, "{}")
    return azure_dispatch_permit(ProviderReservation("direct", "unused", True, money=money))


def _backend(handler: Any) -> AzureResponsesBackend:
    client = openai.OpenAI(
        api_key="fake",
        base_url="https://test.openai.azure.com/openai/v1/",
        max_retries=4,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return AzureResponsesBackend(client, DEPLOYMENTS)


async def test_responses_wire_and_usage_keep_chat_contract_without_sdk_retries() -> None:
    captured = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_response())

    backend = _backend(respond)
    try:
        with scoped_call_policy("goal_text"), _permit(LUNA):
            answer = await backend.complete(
                **_request(
                    model=LUNA,
                    temperature=0.2,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "result", "schema": {"type": "object"}},
                    },
                )
            )
        assert isinstance(answer, Completion)
        assert answer.choices[0].message.content == "answer"
        assert answer.usage and answer.usage.prompt_tokens == 90
        assert answer.usage.completion_tokens == 30
        assert answer.usage.completion_tokens_details.reasoning_tokens == 20
        body = json.loads(captured[0].content)
        assert captured[0].url.path == "/openai/v1/responses"
        assert body["model"] == DEPLOYMENTS[LUNA]
        assert body["max_output_tokens"] == 18000
        assert body["reasoning"] == {"effort": "none"}
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["store"] is False and "temperature" not in body
    finally:
        backend.close()


async def test_provider_429_is_one_http_request_even_if_sdk_was_configured_to_retry() -> None:
    requests = []

    def refuse(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            429, json={"error": {"message": "rate limited", "type": "rate_limit"}}
        )

    backend = _backend(refuse)
    try:
        with pytest.raises(openai.RateLimitError), _permit():
            await backend.complete(**_request())
        assert len(requests) == 1
    finally:
        backend.close()


async def test_gateway_settles_normalized_usage_without_holding_writer_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_AZURE_ENABLED", "true")
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "1")
    monkeypatch.setenv("LLM_AZURE_EXPIRES_AT", "2099-01-04T00:00:00+00:00")
    monkeypatch.setenv("LLM_USD_TO_EUR", "0.88")
    record_azure_allowance()

    def respond(_: httpx.Request) -> httpx.Response:
        with transaction() as conn:
            conn.execute("UPDATE provider_admissions SET tokens=tokens")
        return httpx.Response(200, json=_response())

    backend = _backend(respond)
    try:
        with using_backend(backend):
            await complete_request(_request(), LUNA, byok=False, timeout_seconds=5)
        with connect() as conn:
            tokens = conn.execute("SELECT tokens FROM provider_admissions").fetchall()
        assert len(tokens) == 3 and all(row[0] == 120 for row in tokens)
    finally:
        backend.close()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "name,value",
    [
        ("LLM_ENABLED", "false"),
        ("LLM_AZURE_ENABLED", "false"),
        ("LLM_AZURE_EXPIRES_AT", "2020-01-04T00:00:00+00:00"),
        ("financial_hold", "on"),
    ],
)
async def test_native_dispatch_rechecks_policy_after_thread_wait(
    monkeypatch: pytest.MonkeyPatch, stream: bool, name: str, value: str
) -> None:
    for key, setting in {
        "LLM_ENABLED": "true",
        "LLM_AZURE_ENABLED": "true",
        "LLM_TOTAL_BUDGET_EUR": "1",
        "LLM_AZURE_EXPIRES_AT": "2099-01-04T00:00:00+00:00",
        "LLM_USD_TO_EUR": "0.88",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "fake",
        "AZURE_OPENAI_DEPLOYMENT": DEPLOYMENTS[LUNA],
    }.items():
        monkeypatch.setenv(key, setting)
    record_azure_allowance()
    captured = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_response())

    class MockClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    backend = AzureResponsesBackend.from_environment()

    async def after_wait(function: Any, *args: Any) -> Any:
        if name == "financial_hold":
            from co_scientist.platform.llm.admission.service import current_db_path
            from co_scientist.platform.llm.admission.spend import block_spending

            block_spending(current_db_path())
        else:
            monkeypatch.setenv(name, value)
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", after_wait)
    try:
        with using_backend(backend), pytest.raises(ProviderAdmissionError):
            await complete_request(_request(stream=stream), LUNA, byok=False, timeout_seconds=5)
        assert captured == []
    finally:
        backend.close()


@pytest.mark.parametrize(
    "model",
    [LUNA, LUNA.replace("azure/", "azure/responses/")],
)
@pytest.mark.parametrize("policy", ["disabled", "unset", "expired", "enabled"])
async def test_byok_flag_cannot_exempt_native_deployment_from_funding(
    monkeypatch: pytest.MonkeyPatch, model: str, policy: str
) -> None:
    monkeypatch.setenv("LLM_AZURE_ENABLED", "false" if policy == "disabled" else "true")
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "" if policy == "unset" else "0.000001")
    monkeypatch.setenv(
        "LLM_AZURE_EXPIRES_AT",
        "2020-01-04T00:00:00+00:00" if policy == "expired" else "2099-01-04T00:00:00+00:00",
    )
    captured = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_response())

    backend = _backend(respond)
    try:
        with using_backend(backend), pytest.raises(ProviderAdmissionError):
            await complete_request(
                _request(model=model, api_key="caller-key"), model, byok=True, timeout_seconds=5
            )
        assert captured == []
        with connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM llm_spend").fetchone()[0] == 0
    finally:
        backend.close()


def test_reasoning_and_call_ids_survive_tool_turn_with_repaired_arguments() -> None:
    items: list[dict[str, Any]] = [
        {"type": "reasoning", "id": "reason-one", "summary": [], "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "id": "item-one",
            "call_id": "call-one",
            "name": "search",
            "arguments": "malformed",
        },
    ]
    result = normalize_response(_response(output=items), LUNA)
    history = _message_to_history_dict(result.choices[0].message)
    history["tool_calls"][0]["function"]["arguments"] = '{"query":"short"}'
    converted = response_input(
        [history, {"role": "tool", "tool_call_id": "call-one", "content": "found"}]
    )
    assert converted[0] == items[0]
    assert converted[1]["call_id"] == "call-one" and converted[1]["id"] == "item-one"
    assert converted[1]["arguments"] == '{"query":"short"}'
    assert converted[2] == {
        "type": "function_call_output",
        "call_id": "call-one",
        "output": "found",
    }
    assert items[1]["arguments"] == "malformed"


def test_missing_usage_is_unknown_and_reasoning_is_not_added_to_output() -> None:
    assert normalize_usage(None) is None
    usage = normalize_usage(
        {"output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 20}}
    )
    assert usage and usage.prompt_tokens is None and usage.completion_tokens == 30
    assert (
        normalize_response(
            _response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}), LUNA
        )
        .choices[0]
        .finish_reason
        == "length"
    )
    assert (
        normalize_response(
            _response(
                output=[{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]
            ),
            LUNA,
        )
        .choices[0]
        .finish_reason
        == "content_filter"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "azure/unknown"},
        {"reasoning_effort": "high"},
        {"max_tokens": None},
        {"tools": [{"type": "web_search"}]},
    ],
)
def test_unknown_price_unbounded_output_and_hosted_tools_are_refused(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ProviderAdmissionError):
        response_request(_request(**changes), DEPLOYMENTS)


def test_schema_with_optional_fields_is_sent_non_strict_unless_caller_opts_in() -> None:
    args: dict[str, Any] = _request()
    backend = _backend(lambda request: httpx.Response(500))
    try:
        with using_backend(backend):
            _apply_schema_response_format(args, "answer", LUNA, _SEMANTIC_DECISION_SCHEMA)
    finally:
        backend.close()
    fmt = response_request(args, DEPLOYMENTS)["text"]["format"]
    assert fmt["strict"] is False
    assert set(fmt["schema"]["properties"]) > set(fmt["schema"]["required"])
    strict = {"type": "json_schema", "json_schema": {"name": "r", "schema": {}, "strict": True}}
    body = response_request(_request(response_format=strict), DEPLOYMENTS)
    assert body["text"]["format"]["strict"] is True


def test_nested_hosted_tool_type_cannot_override_function_type() -> None:
    function = {"type": "web_search", "name": "lookup", "parameters": {"type": "object"}}
    body = response_request(
        _request(tools=[{"type": "function", "function": function}]), DEPLOYMENTS
    )
    assert body["tools"] == [
        {"type": "function", "name": "lookup", "parameters": {"type": "object"}}
    ]


async def test_stream_normalizes_text_tool_fragments_reasoning_items_and_final_usage() -> None:
    output: list[dict[str, Any]] = [
        {"type": "reasoning", "id": "reason-one", "summary": [], "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "id": "item-one",
            "call_id": "call-one",
            "name": "search",
            "arguments": '{"q":"x"}',
        },
    ]
    events = [
        {"type": "response.output_text.delta", "delta": "hello"},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**output[1], "arguments": ""},
        },
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"q":'},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '"x"}'},
        {"type": "response.output_item.done", "output_index": 1, "item": output[1]},
        {"type": "response.completed", "response": _response(output=output)},
    ]
    wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    backend = _backend(
        lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=wire)
    )
    try:
        with _permit():
            stream = await backend.complete(**_request(stream=True))
        assert isinstance(stream, ResponsesStream)
        chunks = [chunk async for chunk in stream]
        assert chunks[0].choices[0].delta.content == "hello"
        assert chunks[-1].usage and chunks[-1].usage.prompt_tokens == 90
        assert chunks[-1].choices[0].delta.tool_calls[0].responses_items == output
        args = "".join(
            call.function.arguments
            for chunk in chunks
            for call in chunk.choices[0].delta.tool_calls
        )
        assert args == '{"q":"x"}'
    finally:
        backend.close()


async def test_stream_without_terminal_event_does_not_claim_success() -> None:
    backend = _backend(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
        )
    )
    try:
        with _permit():
            stream = await backend.complete(**_request(stream=True))
        assert isinstance(stream, ResponsesStream)
        assert (await stream.__anext__()).choices[0].delta.content == "partial"
        with pytest.raises(LLMTimeoutError):
            await stream.__anext__()
    finally:
        backend.close()


async def test_litellm_retry_knobs_cannot_bypass_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = []

    async def complete(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr("co_scientist.platform.llm.request.backend.litellm.acompletion", complete)
    await LitellmBackend().complete(model="test/model", max_retries=9, num_retries=7)
    assert captured[0]["max_retries"] == captured[0]["num_retries"] == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://test.openai.azure.com",
        "https://test.invalid",
        "https://test.openai.azure.com.example.com",
        "https://openai.azure.com",
        "https://a.b.openai.azure.com",
        "https://test.openai.azure.com/path",
        "https://test.openai.azure.com/openai/v1",
        "https://test.cognitiveservices.azure.com/openai/deployments/x",
        "https://test.openai.azure.com:8443",
        "https://test.services.ai.azure.com:443x",
        "https://test.openai.azure.com?api-version=v1",
        "https://test.openai.azure.com#v1",
        "https://key@test.openai.azure.com",
        "",
    ],
)
def test_factory_refuses_non_resource_endpoints_without_http(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    _azure_environment(monkeypatch, endpoint)
    with pytest.raises(ProviderAdmissionError):
        AzureResponsesBackend.from_environment()


@pytest.mark.parametrize(
    ("endpoint", "host"),
    [
        ("https://test.openai.azure.com", "test.openai.azure.com"),
        ("https://test.services.ai.azure.com/", "test.services.ai.azure.com"),
        (
            "https://Test-Resource.cognitiveservices.azure.com/",
            "test-resource.cognitiveservices.azure.com",
        ),
        ("https://test.openai.azure.com:443", "test.openai.azure.com"),
    ],
)
def test_factory_sends_documented_resource_forms_to_openai_v1(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, host: str
) -> None:
    _azure_environment(monkeypatch, endpoint)
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_response())

    class MockClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    backend = AzureResponsesBackend.from_environment()
    backend._client.responses.create(model=DEPLOYMENTS[LUNA], input="synthetic")
    assert str(sent[0].url) == f"https://{host}/openai/v1/responses"


def _azure_environment(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    for key, setting in {
        "AZURE_OPENAI_ENDPOINT": endpoint,
        "AZURE_OPENAI_API_KEY": "fake",
        "AZURE_OPENAI_DEPLOYMENT": DEPLOYMENTS[LUNA],
    }.items():
        monkeypatch.setenv(key, setting)


@pytest.mark.parametrize("model", [LUNA])
async def test_real_responses_sdk_sends_partitioned_cache_key(model: str) -> None:
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_response())

    backend = _backend(respond)
    request = _request(model=model, prompt_cache_key="run:claims:1")
    with _permit(model):
        await backend.complete(**request)
    assert sent[0]["prompt_cache_key"] == "run:claims:1"
    assert "prompt_cache_options" not in sent[0]
