from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import openai
from openai.types.responses import Response, ResponseStreamEvent

from co_scientist.core.azure_endpoint import resource_origin
from co_scientist.core.exceptions import LLMTimeoutError, ProviderAdmissionError
from co_scientist.platform.llm.admission.spend import (
    claim_azure_dispatch,
    paid_dispatch_config,
    require_enabled,
)
from co_scientist.platform.llm.profile import model_profile
from co_scientist.platform.llm.roles import current_call_policy

LUNA = "azure/gpt-6-luna-2026-09-22"


@dataclass
class Function:
    name: str | None = None
    arguments: str = ""


@dataclass
class ToolCall:
    id: str | None
    function: Function
    index: int = 0
    type: str = "function"
    responses_items: list[dict[str, Any]] | None = None


@dataclass
class Message:
    content: str | None = None
    role: str = "assistant"
    tool_calls: list[ToolCall] = field(default_factory=list)
    responses_items: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None


@dataclass
class Choice:
    message: Message = field(default_factory=Message)
    delta: Message = field(default_factory=Message)
    finish_reason: str | None = None
    index: int = 0


@dataclass
class UsageDetails:
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    prompt_tokens_details: UsageDetails
    completion_tokens_details: UsageDetails


@dataclass
class Completion:
    model: str
    choices: list[Choice]
    usage: Usage | None = None


def _dump(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else dict(value.model_dump(exclude_none=True))


def _number(data: dict[str, Any], name: str) -> int | None:
    value = data.get(name)
    return value if type(value) is int and value >= 0 else None


def normalize_usage(value: Any) -> Usage | None:
    if value is None:
        return None
    data = _dump(value)
    inputs = data.get("input_tokens_details") or {}
    outputs = data.get("output_tokens_details") or {}
    return Usage(
        _number(data, "input_tokens"),
        _number(data, "output_tokens"),
        UsageDetails(
            cached_tokens=_number(inputs, "cached_tokens"),
            cache_write_tokens=_number(inputs, "cache_write_tokens"),
        ),
        UsageDetails(reasoning_tokens=_number(outputs, "reasoning_tokens")),
    )


# Items the provider returned earlier that are replayed as input. Anything else
# (images, files, hosted tool calls) has billing the byte bound does not cover.
_REPLAYED_ITEMS = frozenset({"message", "reasoning", "function_call"})


def _text(content: Any) -> Any:
    if content is not None and not isinstance(content, str):
        raise ProviderAdmissionError("Only text content is allowed on Azure")
    return content


def response_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("responses_items"):
            calls = {call["id"]: call["function"] for call in message.get("tool_calls") or ()}
            for item in message["responses_items"]:
                if item.get("type") not in _REPLAYED_ITEMS:
                    raise ProviderAdmissionError("Unsupported Responses item on Azure")
                if item.get("type") == "function_call":
                    function = calls.get(item["call_id"])
                    if function is not None:
                        items.append({**item, **function})
                else:
                    items.append(item)
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": _text(message.get("content")) or "",
                }
            )
            continue
        if role not in ("user", "system", "developer", "assistant"):
            raise ProviderAdmissionError("Unsupported Responses message role")
        content = _text(message.get("content"))
        if content:
            items.append({"role": role, "content": content})
        for call in message.get("tool_calls") or ():
            items.append(
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
            )
    return items


def _tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        if tool.get("type") != "function":
            raise ProviderAdmissionError("Only local function tools are allowed on Azure")
        function = tool["function"]
        result.append(
            {
                "type": "function",
                **{
                    key: function[key]
                    for key in ("name", "description", "parameters", "strict")
                    if key in function
                },
            }
        )
    return result


def response_request(request: dict[str, Any], deployments: dict[str, str]) -> dict[str, Any]:
    model = str(request["model"]).replace("azure/responses/", "azure/")
    if model not in deployments:
        raise ProviderAdmissionError("Unknown Azure model price or deployment")
    policy = current_call_policy()
    effort = request.get("reasoning_effort", policy.azure_effort)
    if effort not in (model_profile(model).supported_efforts or ()):
        raise ProviderAdmissionError("Unsupported Azure reasoning effort")
    output = request.get("max_completion_tokens", request.get("max_tokens"))
    if type(output) is not int or output <= 0:
        raise ProviderAdmissionError("Azure needs a bounded output allowance")
    if request.get("n", 1) != 1:
        raise ProviderAdmissionError("Only one Azure completion is allowed")
    stream = bool(request.get("stream"))
    summary = stream and policy.role == "interview" and effort != "none"
    body: dict[str, Any] = {
        "model": deployments[model],
        "input": response_input(request["messages"]),
        "max_output_tokens": output,
        "reasoning": {"effort": effort, **({"summary": "auto"} if summary else {})},
        "store": False,
        # Stateless tool turns must carry the provider's encrypted reasoning
        # items; previous_response_id would depend on provider-side storage.
        "include": ["reasoning.encrypted_content"],
        "stream": stream,
    }
    if "prompt_cache_key" in request:
        body["prompt_cache_key"] = request["prompt_cache_key"]
    if "timeout" in request:
        body["timeout"] = request["timeout"]
    fmt = request.get("response_format")
    if fmt:
        body["text"] = {
            "format": (
                # Chat Completions treats an omitted strict as false, but Azure
                # Responses enforces the strict subset and rejects optional keys.
                {"type": "json_schema", "strict": False, **fmt["json_schema"]}
                if fmt.get("type") == "json_schema"
                else {"type": fmt["type"]}
            )
        }
    if request.get("tools"):
        body["tools"] = _tools(request["tools"])
        choice = request.get("tool_choice")
        if choice is not None:
            body["tool_choice"] = (
                {"type": "function", "name": choice["function"]["name"]}
                if isinstance(choice, dict)
                else choice
            )
    return body


def input_bound(body: dict[str, Any]) -> int:
    # Every o200k token spans at least one UTF-8 byte, and ASCII-escaped JSON
    # only adds bytes; the allowances cover role framing and tool rendering.
    billed = {key: body[key] for key in ("input", "tools", "text") if key in body}
    # Replayed encrypted reasoning bills its decrypted tokens, which its bytes
    # do not bound; the producing turn's output cap does in a uniform loop.
    reasoning = sum(1 for item in body.get("input", ()) if item.get("type") == "reasoning")
    return (
        len(json.dumps(billed).encode("utf-8"))
        + 32 * len(body.get("input", ()))
        + 256 * len(body.get("tools", ()))
        + reasoning * int(body.get("max_output_tokens") or 0)
        + 1024
    )


def request_input_bound(request: dict[str, Any]) -> int:
    # Deployment names do not reach the prompt, so the logical name stands in.
    return input_bound(response_request(request, {LUNA: LUNA}))


def normalize_response(response: Any, model: str) -> Completion:
    data = _dump(response)
    status = data.get("status")
    if status not in ("completed", "incomplete"):
        raise LLMTimeoutError("Azure response did not complete; provider outcome may be unknown")
    items = data.get("output") or []
    content: list[str] = []
    calls: list[ToolCall] = []
    refusal = False
    for item in items:
        if item.get("type") == "function_call":
            calls.append(
                ToolCall(item["call_id"], Function(item["name"], item["arguments"]), len(calls))
            )
        for part in item.get("content") or ():
            if part.get("type") == "output_text":
                content.append(part["text"])
            if part.get("type") == "refusal":
                refusal = True
    reason = "tool_calls" if calls else "stop"
    if status == "incomplete":
        why = (data.get("incomplete_details") or {}).get("reason")
        reason = "content_filter" if why == "content_filter" else "length"
    if refusal:
        reason = "content_filter"
        content.clear()
    message = Message("".join(content) or None, tool_calls=calls, responses_items=items)
    return Completion(
        model, [Choice(message=message, finish_reason=reason)], normalize_usage(data.get("usage"))
    )


_END = object()


def _next(iterator: Iterator[Any]) -> Any:
    # StopIteration cannot cross an asyncio Future.
    return next(iterator, _END)


class ResponsesStream:
    def __init__(self, stream: openai.Stream[Any], model: str) -> None:
        self._stream = stream
        self._iterator = iter(stream)
        self._model = model
        self._terminal = False
        self._closed = False
        self._indices: dict[int, int] = {}
        self._arguments: set[int] = set()

    def __aiter__(self) -> ResponsesStream:
        return self

    async def __anext__(self) -> Completion:
        try:
            while True:
                event = await asyncio.to_thread(_next, self._iterator)
                if event is _END:
                    if not self._terminal:
                        raise LLMTimeoutError(
                            "Azure stream ended without final usage; outcome unknown"
                        )
                    raise StopAsyncIteration
                chunk = self._chunk(_dump(event))
                if chunk is not None:
                    return chunk
        except (StopAsyncIteration, asyncio.CancelledError, LLMTimeoutError):
            await self.aclose()
            raise
        except Exception as error:
            await self.aclose()
            raise LLMTimeoutError(
                "Azure stream transport failed; provider outcome unknown"
            ) from error

    def _chunk(self, event: dict[str, Any]) -> Completion | None:
        kind = event.get("type")
        delta = Message()
        finish = None
        usage = None
        if kind == "response.output_text.delta":
            delta.content = event["delta"]
        elif kind == "response.reasoning_summary_text.delta":
            delta.reasoning_content = event["delta"]
        elif kind == "response.output_item.added":
            item = event["item"]
            if item.get("type") != "function_call":
                return None
            index = int(event["output_index"])
            self._indices[index] = len(self._indices)
            delta.tool_calls = [
                ToolCall(item["call_id"], Function(item["name"]), self._indices[index])
            ]
        elif kind == "response.function_call_arguments.delta":
            index = int(event["output_index"])
            self._arguments.add(index)
            delta.tool_calls = [
                ToolCall(None, Function(arguments=event["delta"]), self._indices[index])
            ]
        elif kind == "response.output_item.done":
            item = event["item"]
            index = int(event["output_index"])
            if item.get("type") != "function_call" or index in self._arguments:
                return None
            delta.tool_calls = [
                ToolCall(None, Function(arguments=item["arguments"]), self._indices[index])
            ]
        elif kind in ("response.completed", "response.incomplete"):
            response = normalize_response(event["response"], self._model)
            self._terminal = True
            finish = response.choices[0].finish_reason
            usage = response.usage
            delta.responses_items = response.choices[0].message.responses_items
            delta.tool_calls = [
                ToolCall(None, Function(), call.index, responses_items=delta.responses_items)
                for call in response.choices[0].message.tool_calls
            ]
        elif kind in ("error", "response.failed"):
            raise LLMTimeoutError("Azure stream failed; provider outcome may be unknown")
        else:
            return None
        return Completion(self._model, [Choice(delta=delta, finish_reason=finish)], usage)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await asyncio.to_thread(self._stream.close)


class AzureResponsesBackend:
    """A synchronous SDK client crosses worker loops safely. Retry policy
    remains at the counted gateway boundary.
    """

    def __init__(
        self, client: openai.OpenAI, deployments: dict[str, str], *, operator_funded: bool = False
    ) -> None:
        self._client = client.with_options(max_retries=0)
        self._deployments = dict(deployments)
        self._operator_funded = operator_funded

    @classmethod
    def from_environment(cls) -> AzureResponsesBackend:
        endpoint = resource_origin(os.getenv("AZURE_OPENAI_ENDPOINT", ""))
        if endpoint is None:
            raise ProviderAdmissionError("Azure needs a resource HTTPS endpoint")
        key = os.getenv("AZURE_OPENAI_API_KEY", "")
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
        if not key or not deployment:
            raise ProviderAdmissionError("An Azure deployment and a credential are required")
        client = openai.OpenAI(
            api_key=key,
            base_url=endpoint + "/openai/v1/",
            max_retries=0,
            http_client=httpx.Client(follow_redirects=False),
        )
        return cls(client, {LUNA: deployment}, operator_funded=True)

    def close(self) -> None:
        self._client.close()

    def supports_json_schema(self, model_name: str) -> bool:
        return model_name in self._deployments

    async def complete(self, **completion_args: Any) -> Completion | ResponsesStream:
        request = response_request(completion_args, self._deployments)
        model = str(completion_args["model"]).replace("azure/responses/", "azure/")
        stream = request.pop("stream")
        try:
            if stream:
                events = await asyncio.to_thread(self._stream_request, request, model)
                return ResponsesStream(events, model)
            response = await asyncio.to_thread(self._response_request, request, model)
        except (openai.APIConnectionError, openai.APITimeoutError) as error:
            raise LLMTimeoutError(
                "Azure transport failed; provider outcome may be unknown"
            ) from error
        return normalize_response(response, model)

    def _dispatch_guard(self, request: dict[str, Any], model: str) -> None:
        # A thread can become runnable after the gateway's policy check.
        require_enabled()
        if self._operator_funded:
            from co_scientist.platform.llm.admission.service import current_db_path

            paid_dispatch_config(current_db_path())
        claim_azure_dispatch(model, input_bound(request), request["max_output_tokens"])

    def _stream_request(
        self, request: dict[str, Any], model: str
    ) -> openai.Stream[ResponseStreamEvent]:
        self._dispatch_guard(request, model)
        return cast(
            openai.Stream[ResponseStreamEvent],
            self._client.responses.create(stream=True, **request),
        )

    def _response_request(self, request: dict[str, Any], model: str) -> Response:
        self._dispatch_guard(request, model)
        return cast(Response, self._client.responses.create(stream=False, **request))
