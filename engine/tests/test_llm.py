from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from litellm.exceptions import BadRequestError

import co_scientist.science.generation.literature_tools.validate as vs
from co_scientist.core.exceptions import (
    LLMCallBudgetExceededError,
    LLMRateLimitParkError,
)
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
    complete_request,
    scoped_telemetry,
)
from co_scientist.platform.llm.request import backend
from co_scientist.platform.llm.structured.validate import attempt_json_repair
from co_scientist.platform.retrieval.mcp_client import MCPToolClient
from co_scientist.platform.retrieval.tools.provider import MCPToolProvider
from co_scientist.science.reflection import review as rv
from tests._llm_fake import (
    FakeBackend,
    install_fake_backend,
    make_completion,
    make_message,
    scripted_backend,
)
from tests._mcp import make_tool_call
from tests._state import make_hypothesis, make_state

_FENCED = '```json\n{"a": 1}\n```'


_LATEX = r'{"experiment": "use GFP-Ub\(^{G76V}\) reporter"}'


@pytest.mark.parametrize(
    ("text", "major", "expected"),
    [
        ('{"a": 1, "b": "x"}', False, ({"a": 1, "b": "x"}, False)),
        ('  {"a": 1}  ', False, ({"a": 1}, False)),
        ("{}", True, ({}, False)),
        ("[1, 2, 3]", False, ([1, 2, 3], False)),
        ("[]", False, (None, False)),
        ('{"a": 1,}', False, ({"a": 1}, False)),
        ('{"a": [1, 2,]}', False, ({"a": [1, 2]}, False)),
        (_FENCED, False, (None, False)),
        (_FENCED, True, ({"a": 1}, True)),
        ("{'a': 1}", True, (None, False)),
        ('{"a": 1, "b": 2', False, (None, False)),
        ('{"a": 1, "b": 2', True, ({"a": 1, "b": 2}, True)),
        ('{"a": "hello', True, ({"a": "hello"}, True)),
        ('{"items": ["x", "y', True, ({"items": ["x", "y"]}, True)),
        ('{"a": {"b": 1', True, ({"a": {"b": 1}}, True)),
        ("[1, 2, 3", True, ([1, 2, 3], True)),
        ('Here is: {"a": 1} thanks', False, (None, False)),
        ('Here is: {"a": 1} thanks', True, ({"a": 1}, True)),
        ("this is not json at all", True, (None, False)),
        ("   ", True, (None, False)),
        (
            _LATEX,
            False,
            ({"experiment": r"use GFP-Ub\(^{G76V}\) reporter"}, False),
        ),
        (
            r'{"a": "x\(y", "b": 1,}',
            False,
            ({"a": r"x\(y", "b": 1}, False),
        ),
        (
            r'{"a": "line\n\t\"q\"\\done"}',
            False,
            ({"a": 'line\n\t"q"\\done'}, False),
        ),
    ],
)
def test_json_repair_fixes_minor_flaws_and_gates_major_ones(
    text: str, major: bool, expected: tuple[Any, bool]
) -> None:
    assert attempt_json_repair(text, allow_major_repairs=major) == expected


class FakeMCPClient:
    def __init__(self, tools: dict[str, Any] | None = None) -> None:
        self._tools = tools or {}
        self.get_tools_calls: list[list[str] | None] = []
        self.executed: list[Any] = []

    def get_tools(
        self, whitelist: list[str] | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.get_tools_calls.append(whitelist)
        selected = {
            name: obj for name, obj in self._tools.items() if whitelist is None or name in whitelist
        }
        schemas = [{"type": "function", "function": {"name": name}} for name in selected]
        return selected, schemas

    async def execute_tool_call(self, tool_call: Any) -> dict[str, Any]:
        self.executed.append(tool_call)
        return {
            "role": "tool",
            "name": tool_call.function.name,
            "tool_call_id": tool_call.id,
            "content": "mcp-result",
        }


class FailingMCPClient(FakeMCPClient):
    async def execute_tool_call(self, tool_call: Any) -> dict[str, Any]:
        raise RuntimeError(f"server unavailable for {tool_call.function.name}")


def _provider(fake: FakeMCPClient) -> MCPToolProvider:
    provider = MCPToolProvider(mcp_client=cast(MCPToolClient, fake))
    provider.get_tools(mcp_whitelist=["pubmed_search"])
    return provider


async def test_a_known_tool_call_is_delegated_and_counted() -> None:
    fake = FakeMCPClient({"pubmed_search": object()})
    provider = _provider(fake)
    executor, counts = provider.tracked_executor("Draft")
    call = make_tool_call("pubmed_search", json.dumps({"query": "cancer"}), call_id="call-mcp")

    result = await executor(call)
    await executor(make_tool_call("pubmed_search", "{}"))

    assert (result["name"], result["tool_call_id"]) == (
        "pubmed_search",
        "call-mcp",
    )
    assert result["content"] == "mcp-result"
    assert counts == {"pubmed_search": 2}
    assert fake.executed[0] is call


async def test_an_unknown_tool_or_failing_client_becomes_an_error_result() -> None:
    unknown = await _provider(FakeMCPClient()).execute_tool_call(
        make_tool_call("nope_tool", "{}", call_id="call-x")
    )
    assert unknown["role"] == "tool"
    assert unknown["tool_call_id"] == "call-x"
    assert json.loads(unknown["content"])["error"] == "unknown tool: nope_tool"

    failing = await _provider(FailingMCPClient({"pubmed_search": object()})).execute_tool_call(
        make_tool_call("pubmed_search", "{}")
    )
    error = json.loads(failing["content"])["error"]
    assert "tool execution failed" in error
    assert "server unavailable" in error

    provider = _provider(FakeMCPClient({"pubmed_search": object()}))
    provider.mcp_client = None
    unconfigured = await provider.execute_tool_call(make_tool_call("pubmed_search", "{}"))
    assert "MCP client not configured" in unconfigured["content"]


async def test_usage_is_recorded_once_after_the_stream_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = SimpleNamespace(
        model="gpt-4o-mini",
        choices=[],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3),
    )

    async def chunks() -> AsyncIterator[Any]:
        yield SimpleNamespace(choices=[], usage=None)
        yield final

    async def provider(**kwargs: Any) -> Any:
        return chunks()

    install_fake_backend(monkeypatch, provider)
    with scoped_telemetry("app_stream") as telemetry:
        response = await complete_request(
            {"model": "gpt-4o-mini", "stream": True},
            "gpt-4o-mini",
            byok=False,
            timeout_seconds=1,
        )
        assert telemetry.snapshot() == {}
    assert len([chunk async for chunk in response]) == 2
    await response.aclose()
    usage = telemetry.snapshot()["app_stream::gpt-4o-mini"]
    assert usage["calls"] == 1
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 3
    assert usage["reported_usage_calls"] == 1
    assert usage["errors"] == {}


_INT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
}


async def test_a_rejected_reasoning_cap_falls_back_to_the_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported bounds degrade to a reasoning tier instead of failing."""
    model = "openrouter/minimax/minimax-m3:free"
    backend = scripted_backend(
        monkeypatch,
        [
            BadRequestError(
                message=("reasoning.max_tokens is not supported for this model."),
                model=model,
                llm_provider="openrouter",
            ),
            make_completion(make_message('{"a": 1}')),
        ],
    )

    result = await call_llm_json(
        "a prompt",
        CompletionSpec(model_name=model, max_tokens=12000, json_schema=_INT_SCHEMA),
        max_attempts=3,
        options=LLMCallOptions(enable_thinking=False),
    )

    assert result == {"a": 1}
    first, second = (r["extra_body"]["reasoning"] for r in backend.requests)
    assert first["max_tokens"] > 0
    assert second == {"enabled": True, "effort": "low"}


async def _answers_ok(**_kwargs: Any) -> str:
    return "ok"


def test_a_backend_scope_restores_what_it_replaced_even_when_it_raises() -> None:
    assert isinstance(backend.active_backend(), backend.LitellmBackend)
    first, second = FakeBackend(_answers_ok), FakeBackend(_answers_ok)

    previous = backend.install_backend(first)
    try:
        assert previous is None
        assert backend.install_backend(second) is first
    finally:
        backend.install_backend(previous)
    assert isinstance(backend.active_backend(), backend.LitellmBackend)

    with pytest.raises(RuntimeError), backend.using_backend(first):
        assert backend.active_backend() is first
        raise RuntimeError("boom")
    assert isinstance(backend.active_backend(), backend.LitellmBackend)


PARK = LLMRateLimitParkError(1788825600.0, "message_per_day")


OVER_BUDGET = LLMCallBudgetExceededError(2501, 2500)


ORDINARY = ValueError("provider returned unparseable JSON")


async def _initial_reviews(monkeypatch: pytest.MonkeyPatch, error: Exception) -> Any:
    monkeypatch.setattr(rv, "review_single_hypothesis", AsyncMock(side_effect=error))
    return await rv.review_parallel_individual(
        [make_hypothesis(text="a"), make_hypothesis(text="b")],
        rv.ReviewContext.from_state(make_state()),
    )


@pytest.mark.parametrize(
    ("error", "degrades"),
    [(ORDINARY, True), (PARK, False), (OVER_BUDGET, False)],
)
async def test_initial_reviews_degrade_ordinary_failures_but_not_spent_caps(
    monkeypatch: pytest.MonkeyPatch, error: Exception, degrades: bool
) -> None:
    """Batch fallback buys per-item retries; swallowing a spent cap
    multiplies doomed requests."""
    if degrades:
        assert await _initial_reviews(monkeypatch, error) == [None, None]
    else:
        with pytest.raises(type(error)):
            await _initial_reviews(monkeypatch, error)


def test_a_synthesis_batch_is_retried_individually_unless_a_cap_is_spent() -> None:
    batches = [[{"a": 1}], [{"b": 2}]]
    validated, failed = vs._partition_synthesis_results(
        batches, [[{"ok": True}], RuntimeError("batch refused")]
    )
    assert (validated, failed) == ([{"ok": True}], [(1, batches[1])])

    for error in (PARK, OVER_BUDGET):
        with pytest.raises(type(error)):
            vs._partition_synthesis_results(batches, [[], error])


_SOURCE_DIR = pathlib.Path(__file__).resolve().parents[1] / "src/co_scientist"
_AGENT_DIRS = (_SOURCE_DIR / "science",)


_LLM_CALLS = {"call_llm", "call_llm_json", "call_llm_with_tools"}


_GUARD = "TASK_CONTROL_FLOW_ERRORS"


def _calls_an_llm(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id in _LLM_CALLS
        for child in ast.walk(node)
    )


def _catches_bare_exception(handler: ast.ExceptHandler) -> bool:
    return isinstance(handler.type, ast.Name) and handler.type.id == "Exception"


def _unguarded_handlers(node: ast.Try) -> list[int]:
    """A re-raise after a broad handler cannot run; guard order matters."""
    offenders = []
    guarded = False
    for handler in node.handlers:
        if _GUARD in ast.dump(handler.type or ast.Pass()):
            guarded = True
        elif _catches_bare_exception(handler) and not guarded:
            offenders.append(handler.lineno)
    return offenders


def _unguarded_llm_fallbacks(tree: ast.AST) -> list[int]:
    return [
        line
        for node in ast.walk(tree)
        if isinstance(node, ast.Try) and _calls_an_llm(node)
        for line in _unguarded_handlers(node)
    ]


def test_no_agent_degrades_an_llm_call_over_a_control_flow_error() -> None:
    unguarded: dict[str, list[int]] = {}
    for path in sorted(p for d in _AGENT_DIRS for p in d.rglob("*.py")):
        offenders = _unguarded_llm_fallbacks(ast.parse(path.read_text(encoding="utf-8")))
        if offenders:
            unguarded[str(path.relative_to(_SOURCE_DIR))] = offenders

    assert not unguarded, (
        f"these handlers swallow a rate-limit park or the run's call-budget ceiling: {unguarded}"
    )

    bare = "try:\n    await call_llm_json(p)\nexcept Exception:\n    pass\n"
    guarded = (
        "try:\n    await call_llm_json(p)\n"
        "except TASK_CONTROL_FLOW_ERRORS:\n    raise\n"
        "except Exception:\n    pass\n"
    )
    assert _unguarded_llm_fallbacks(ast.parse(bare)) == [3]
    assert _unguarded_llm_fallbacks(ast.parse(guarded)) == []


def test_importing_a_foundation_module_first_does_not_cycle() -> None:
    """Only a fresh interpreter exposes cycles involving a half-initialized
    module."""
    result = subprocess.run(
        [sys.executable, "-c", "import co_scientist.platform.llm.precall"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
