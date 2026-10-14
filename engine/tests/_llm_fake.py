from __future__ import annotations

import asyncio
import copy
import itertools
import json
import logging
import time
import types
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from litellm.exceptions import APIError, BadRequestError, RateLimitError
from litellm.exceptions import ContextWindowExceededError as ContextWindow
from litellm.exceptions import Timeout as LiteLLMTimeout

from co_scientist.core.exceptions import LLMCallBudgetExceededError
from co_scientist.orchestration.generator.core import HypothesisGenerator
from co_scientist.orchestration.generator.run_setup import GeneratorOptions
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    ToolLoop,
    call_llm,
    call_llm_json,
    call_llm_with_tools,
    rate_limited_attempt_count,
    scoped_telemetry,
)
from co_scientist.platform.llm.offline.llm import (
    _ARRAY_LENGTH_HINTS,
    _fill_schema,
    _FillHints,
    _prompt_text,
    _supervisor_allocation_response,
)
from co_scientist.platform.llm.offline.llm import _build_response as _fake_response
from co_scientist.platform.llm.request import backend

# Shared across every fake call in a test run so no two generated leaves
# (hypothesis text, free-form turns, etc.) ever collide.
_counter = itertools.count(1)


def _next_leaf(_field: str = "") -> str:
    """Globally unique leaves prevent generated hypotheses from colliding."""
    return f"stub-{next(_counter)}"


Respond = Callable[..., Awaitable[Any]]


class FakeBackend:
    def __init__(
        self,
        respond: Respond,
        *,
        requests: list[dict[str, Any]] | None = None,
        supports_json_schema: Callable[[str], bool] | None = None,
    ) -> None:
        self._respond = respond
        self.requests: list[dict[str, Any]] = [] if requests is None else requests
        self._supports = supports_json_schema

    async def complete(self, **completion_args: Any) -> Any:
        self.requests.append(completion_args)
        return await self._respond(**completion_args)

    def supports_json_schema(self, model_name: str) -> bool:
        if self._supports is not None:
            return self._supports(model_name)
        return backend.litellm_supports_json_schema(model_name)


def restore_backend_at_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatching the slot to itself registers restoration of the current
    backend."""
    monkeypatch.setattr(backend, "_installed", backend._installed)


def install_fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    respond: Respond,
    *,
    requests: list[dict[str, Any]] | None = None,
    supports_json_schema: Callable[[str], bool] | None = None,
) -> FakeBackend:
    fake = FakeBackend(respond, requests=requests, supports_json_schema=supports_json_schema)
    restore_backend_at_teardown(monkeypatch)
    backend.install_backend(fake)
    return fake


def scripted_backend(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any],
    *,
    repeat_last: bool = False,
) -> FakeBackend:
    queue = iter(responses)

    async def respond(**_kwargs: Any) -> Any:
        item = (
            responses[min(len(fake.requests) - 1, len(responses) - 1)]
            if repeat_last
            else next(queue)
        )
        if isinstance(item, Exception):
            raise item
        return item

    fake = install_fake_backend(monkeypatch, respond)
    return fake


def stub_call_llm_json(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType,
    response: dict[str, Any],
    *,
    copy_response: bool = False,
) -> list[dict[str, Any]]:
    """Patch the consumer namespace because nodes import their collaborators
    by name."""
    calls: list[dict[str, Any]] = []

    async def fake(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if args:
            kwargs = {"prompt": args[0], **kwargs}
        calls.append(kwargs)
        return copy.deepcopy(response) if copy_response else response

    monkeypatch.setattr(module, "call_llm_json", fake)
    return calls


def mock_call_llm_json(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType,
    response: dict[str, Any] | None = None,
    *,
    side_effect: Exception | Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> AsyncMock:
    fake = AsyncMock(return_value=response, side_effect=side_effect)
    monkeypatch.setattr(module, "call_llm_json", fake)
    return fake


def make_test_generator() -> HypothesisGenerator:
    return HypothesisGenerator(
        model_name="fake/model",
        max_iterations=1,
        initial_hypotheses_count=2,
        evolution_max_count=2,
        options=GeneratorOptions(
            tournament_pairs=2,
        ),
    )


async def _fake_acompletion(**kwargs: Any) -> Any:
    response_format = kwargs.get("response_format")
    if response_format and response_format.get("type") == "json_schema":
        json_schema = response_format["json_schema"]
        schema = json_schema["schema"]
        if json_schema.get("name") == "supervisor_allocation":
            content = _supervisor_allocation_response(_prompt_text(kwargs))
            return _fake_response(content)
        length_hint = _ARRAY_LENGTH_HINTS.get(json_schema.get("name", ""))
        hints = _FillHints(array_lengths=(length_hint(_prompt_text(kwargs)) if length_hint else {}))
        content = json.dumps(_fill_schema(schema, _next_leaf, hints))
    elif response_format and response_format.get("type") == "json_object":
        content = "{}"
    else:
        content = f"free-form response {next(_counter)}"
    return _fake_response(content)


def install_fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_backend(
        monkeypatch,
        _fake_acompletion,
        # Fake answers read schema objects, not the json_object prompt shim.
        # The gateway downgrade has separate coverage.
        supports_json_schema=lambda _model_name: True,
    )


NESTED_SCHEMA: dict[str, Any] = {
    "name": "capability_shim_test",
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "assessment": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["holds", "weakened"],
                    },
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["verdict", "notes"],
            },
        },
        "required": ["summary", "assessment"],
    },
}


def make_message(
    content: str | None,
    tool_calls: list[Any] | None = None,
    role: str = "assistant",
) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, tool_calls=tool_calls)


def make_usage(
    prompt_tokens: int, completion_tokens: int, reasoning_tokens: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )


def make_completion(
    message: SimpleNamespace,
    usage: SimpleNamespace | None = None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    choice = SimpleNamespace(message=message)
    if finish_reason is not None:
        choice.finish_reason = finish_reason
    return SimpleNamespace(choices=[choice], usage=usage)


def make_tool_call(call_id: str, name: str, arguments: Any) -> SimpleNamespace:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def patch_acompletion(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[SimpleNamespace],
    recorder: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    state = {"calls": 0}
    queue = iter(responses)

    async def fake_acompletion(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        state["calls"] += 1
        return next(queue)

    install_fake_backend(monkeypatch, fake_acompletion, requests=recorder)
    return state


# The wrapper only reads the shared schema, so tests can safely reuse it.
SEARCH_TOOL: list[dict[str, Any]] = [{"type": "function", "function": {"name": "search"}}]

_MODEL = "deepseek/deepseek-v4-flash"
# Not a declared gateway model, so the rung that answers a mandatory
# reasoning refusal is observable on the wire as a reasoning knob.
GATEWAY_MODEL = "openrouter/deepseek/deepseek-v4-flash"

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
}

SCHEMA_FEEDBACK = "VALIDATION ERROR FROM PREVIOUS ATTEMPT"

# What each retry line is called below. A message matching none of these at
# WARNING or above is reported as ``other`` so a new line cannot slip in.
_LINE_KINDS: tuple[tuple[str, str], ...] = (
    ("LLM call failed", "failed"),
    ("waiting", "waited"),
    ("not retrying", "terminal"),
    ("Error in LLM tool call loop", "tool-terminal"),
    ("Platform rate limit", "park"),
    ("retrying with", "escalated"),
    ("Schema validation failed", "schema"),
    ("retrying llm call", "retry"),
    ("added validation feedback", "feedback-debug"),
)

Line = tuple[str, str]


async def echo_executor(tc: Any) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tc.id, "content": "result"}


@dataclass(frozen=True)
class Entry:
    kind: str
    model: str = _MODEL
    options: LLMCallOptions | None = None
    executor: Callable[[Any], Awaitable[dict[str, Any]]] = echo_executor

    async def __call__(self, max_attempts: int) -> Any:
        spec = CompletionSpec(model_name=self.model, max_tokens=8000)
        if self.kind == "text":
            return await call_llm("a prompt", spec, self.options, max_attempts)
        if self.kind == "json":
            spec = replace(spec, json_schema=_SCHEMA)
            return await call_llm_json("a prompt", spec, max_attempts, self.options)
        loop = ToolLoop(tools=SEARCH_TOOL, executor=self.executor)
        return await call_llm_with_tools("a prompt", spec, loop, self.options)


TEXT = Entry("text")
JSON = Entry("json")
TOOLS = Entry("tools")


@dataclass
class Run:
    calls: list[dict[str, Any]]
    slept: list[float]
    error: Exception | None
    result: Any
    lines: list[Line]
    throttled: int
    retries: int

    @property
    def max_tokens(self) -> list[int]:
        return [call["max_tokens"] for call in self.calls]

    @property
    def thinking(self) -> list[str]:
        return [call["extra_body"]["thinking"]["type"] for call in self.calls]

    @property
    def reasoning(self) -> list[Any]:
        return [call["extra_body"].get("reasoning") for call in self.calls]

    @property
    def prompts(self) -> list[str]:
        return [call["messages"][0]["content"] for call in self.calls]

    @property
    def logged(self) -> list[Line]:
        return [line for line in self.lines if line[1] in {"WARNING", "ERROR", "CRITICAL"}]

    @property
    def retry_announcements(self) -> list[Line]:
        return [line for line in self.lines if line[0] == "retry"]


def _classify(record: logging.LogRecord) -> Line | None:
    text = record.getMessage()
    for needle, kind in _LINE_KINDS:
        if needle in text:
            return kind, record.levelname
    if record.levelno >= logging.WARNING:
        return f"other: {text[:50]}", record.levelname
    return None


class Driver:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        self._monkeypatch = monkeypatch
        self._caplog = caplog

    async def __call__(self, entry: Entry, script: list[Any], max_attempts: int = 3) -> Run:
        calls: list[dict[str, Any]] = []
        slept: list[float] = []
        queue = list(script)

        async def provider(**kwargs: Any) -> Any:
            # A snapshot: the tool loop keeps appending to its transcript.
            calls.append(copy.deepcopy(kwargs))
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        install_fake_backend(self._monkeypatch, provider)
        self._monkeypatch.setattr(asyncio, "sleep", sleep)
        throttled_before = rate_limited_attempt_count()
        error: Exception | None = None
        result: Any = None
        with (
            scoped_telemetry("characterization") as telemetry,
            self._caplog.at_level(logging.DEBUG, logger="co_scientist"),
        ):
            try:
                result = await entry(max_attempts)
            except Exception as exc:
                error = exc
        records = self._caplog.records
        return Run(
            calls=calls,
            slept=slept,
            error=error,
            result=result,
            lines=[k for r in records if (k := _classify(r)) is not None],
            throttled=rate_limited_attempt_count() - throttled_before,
            retries=sum(usage["retries"] for usage in telemetry.snapshot().values()),
        )


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Driver:
    return Driver(monkeypatch, caplog)


def ok(entry: Entry) -> SimpleNamespace:
    text = '{"a": 1}' if entry.kind == "json" else "fine"
    return make_completion(make_message(text))


def rate_limited(reset_in: float | None = None) -> RateLimitError:
    response = None
    if reset_in is not None:
        reset_ms = int((time.time() + reset_in) * 1000)
        response = httpx.Response(
            status_code=429,
            headers={"x-ratelimit-reset": str(reset_ms)},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1"),
        )
    return RateLimitError(
        message="RateLimitError: OpenrouterException - rate limited",
        llm_provider="openrouter",
        model="m",
        response=response,
    )


def overloaded() -> APIError:
    return APIError(
        status_code=500,
        message=(
            "litellm.APIError: OpenrouterException - Upstream error from "
            "Nvidia: Service temporarily overloaded"
        ),
        llm_provider="openrouter",
        model="m",
    )


def reasoning_mandatory() -> BadRequestError:
    return BadRequestError(
        message=(
            'OpenrouterException - {"error":{"message":"Reasoning is '
            'mandatory for this endpoint and cannot be disabled.",'
            '"code":400}}'
        ),
        model=GATEWAY_MODEL,
        llm_provider="openrouter",
    )


def exhausted() -> SimpleNamespace:
    return make_completion(
        make_message(None),
        usage=make_usage(500, 18000, reasoning_tokens=18000),
        finish_reason="length",
    )


def thinking_only() -> SimpleNamespace:
    """More room cannot fix thinking-only output; recovery must change
    reasoning."""
    return make_completion(
        make_message(None),
        usage=make_usage(500, 1149, reasoning_tokens=1149),
        finish_reason="stop",
    )


def wrong_type() -> SimpleNamespace:
    return make_completion(make_message('{"a": "not a number"}'))


def call_ceiling() -> LLMCallBudgetExceededError:
    return LLMCallBudgetExceededError(count=7, ceiling=5)


def timed_out() -> LiteLLMTimeout:
    return LiteLLMTimeout(message="read timed out", model="m", llm_provider="p")


def too_big() -> ContextWindow:
    return ContextWindow(message="too many tokens", model="m", llm_provider="p")


WAITED_THEN_GAVE_UP: list[Line] = [
    ("failed", "WARNING"),
    ("waited", "WARNING"),
    ("failed", "WARNING"),
    ("waited", "WARNING"),
    ("failed", "ERROR"),
]


@pytest.fixture
def _free_catalog(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from co_scientist.platform.llm.admission import free_policy as free_catalog

    monkeypatch.delenv("COSCIENTIST_REQUIRE_FREE_MODELS", raising=False)
    monkeypatch.setattr(free_catalog, "_reader", free_catalog.CatalogReader())
    yield


def _catalog(pricing: Any) -> dict[str, Any]:
    return {
        "data": [
            {
                "id": "free/zero:free",
                "pricing": pricing,
                "architecture": {
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                },
            }
        ]
    }


def _mock_catalog(monkeypatch: pytest.MonkeyPatch, data: Any) -> list[str]:
    calls: list[str] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(url)
        assert kwargs == {"timeout": 15, "trust_env": False}
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    return calls
