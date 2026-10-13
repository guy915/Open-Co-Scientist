from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from co_scientist.orchestration.task_runtime import (
    plan_portfolio,
)
from co_scientist.platform.llm import tool_effects
from co_scientist.platform.llm.tool_effects import (
    is_barrier,
    parse_effects,
    resolve_tool_effects,
)
from co_scientist.platform.llm.tools.loop import (
    _execute_logged_tool,
    _execute_tool_calls,
)
from tests._llm_fake import (
    make_tool_call,
)
from tests._state import make_state


def test_plan_portfolio_never_calls_the_orchestrator_resolver() -> None:
    """next_task still holds the previous cycle until the orchestrator
    overwrites it."""
    state = make_state(mcp_available=False, next_task="evolve")
    assert plan_portfolio("proximity", state) == [
        "proximity",
        "orchestrator",
    ]


def _call(name: str) -> SimpleNamespace:
    return make_tool_call(f"call_{name}", name, "{}")


def test_resolve_treats_a_broken_registry_as_a_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import co_scientist.platform.retrieval.config.registry as registry

    def _boom() -> Any:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(registry, "get_tool_registry", _boom)
    assert is_barrier(resolve_tool_effects("pubmed_search"))


def _patch_effects(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    monkeypatch.setattr(
        tool_effects,
        "resolve_tool_effects",
        lambda name: parse_effects(mapping.get(name, [])),
    )


@pytest.mark.asyncio
async def test_barrier_tool_never_overlaps_a_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_effects(
        monkeypatch,
        {"read_a": ["read"], "spawn": ["process"], "read_b": ["read"]},
    )
    in_flight = 0
    overlapped_with_barrier = False

    async def executor(tool_call: Any) -> dict[str, Any]:
        nonlocal in_flight, overlapped_with_barrier
        name = tool_call.function.name
        in_flight += 1
        if name == "spawn" and in_flight > 1:
            overlapped_with_barrier = True
        # Yield so a genuinely concurrent sibling can be observed.
        await asyncio.sleep(0)
        in_flight -= 1
        return {"role": "tool", "content": name}

    results = await _execute_tool_calls(
        [_call("read_a"), _call("spawn"), _call("read_b")], executor
    )

    assert not overlapped_with_barrier
    assert [r["content"] for r in results] == ["read_a", "spawn", "read_b"]


@pytest.mark.parametrize("outcome", ["returned", "failed", "cancelled"])
async def test_tool_diagnostics_preserve_outcome_without_arguments_or_output(
    caplog: pytest.LogCaptureFixture, outcome: str
) -> None:
    call = make_tool_call("call-id", "search_literature", '{"query": "private request"}')
    calls: list[Any] = []

    async def execute(value: Any) -> dict[str, Any]:
        calls.append(value)
        if outcome == "failed":
            raise ValueError("private error")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return {"role": "tool", "content": "private result"}

    with caplog.at_level(logging.INFO, logger="co_scientist.platform.llm.tools.loop"):
        if outcome == "failed":
            with pytest.raises(ValueError):
                await _execute_logged_tool(call, execute)
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await _execute_logged_tool(call, execute)
        else:
            assert await _execute_logged_tool(call, execute) == {
                "role": "tool",
                "content": "private result",
            }
    assert calls == [call]
    assert "name=search_literature" in caplog.text
    assert f"outcome={outcome}" in caplog.text and "duration_seconds=" in caplog.text
    assert "private" not in caplog.text
