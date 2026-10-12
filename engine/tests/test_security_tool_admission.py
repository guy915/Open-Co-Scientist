from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from co_scientist.core.exceptions import GenerationError
from co_scientist.orchestration.engine_tasks.fanout import _generation_task_specs
from co_scientist.orchestration.generator.core import HypothesisGenerator
from co_scientist.orchestration.generator.run_setup import GeneratorOptions
from co_scientist.platform.llm.tool_effects import ToolEffect, declare_local_tool
from co_scientist.platform.llm.tools.loop import (
    MAX_CONCURRENT_TOOLS,
    MAX_TOOL_CALLS_PER_TURN,
    _execute_tool_calls,
)
from co_scientist.science.generation.operations import prepare_generation
from tests._llm_fake import make_tool_call
from tests._state import make_state


async def test_oversized_generation_checkpoint_never_reaches_allocation() -> None:
    state = make_state(supervisor_guidance={"plan": "ready"}, initial_hypotheses_count=10**12)
    with pytest.raises(GenerationError, match="budget"):
        await prepare_generation(state)
    with pytest.raises(ValueError, match="budget"):
        _generation_task_specs({"debate_only": 10**12})
    assert len(_generation_task_specs({"debate_only": 96})) == 96


def test_direct_generator_cannot_bypass_planning_limits() -> None:
    with pytest.raises(ValueError, match="initial_hypotheses_count"):
        HypothesisGenerator(initial_hypotheses_count=10**12)
    with pytest.raises(ValueError, match="max_iterations"):
        HypothesisGenerator(options=GeneratorOptions(budget={"max_iterations": 10**12}))
    generator = HypothesisGenerator(options=GeneratorOptions(budget={"max_iterations": 8}))
    assert generator.budget["max_iterations"] == 8


async def test_tool_limit_preserves_pairing_and_bounds_concurrent_reads() -> None:
    declare_local_tool("security_read", ToolEffect.READ | ToolEffect.NETWORK)
    calls = [make_tool_call(str(i), "security_read", "{}") for i in range(100)]
    active = peak = executed = 0

    async def execute(call: Any) -> dict[str, Any]:
        nonlocal active, peak, executed
        active += 1
        peak = max(peak, active)
        executed += 1
        try:
            await asyncio.sleep(0)
            return {"role": "tool", "tool_call_id": call.id, "content": "{}"}
        finally:
            active -= 1

    results = await _execute_tool_calls(calls, execute)
    assert executed == MAX_TOOL_CALLS_PER_TURN
    assert peak == MAX_CONCURRENT_TOOLS
    assert [result["tool_call_id"] for result in results] == [call.id for call in calls]
    assert all("error" in json.loads(result["content"]) for result in results[executed:])


async def test_bounded_batches_preserve_write_barriers() -> None:
    declare_local_tool("security_read", ToolEffect.READ)
    declare_local_tool("security_write", ToolEffect.WRITE)
    active = 0
    order: list[str] = []

    async def execute(call: Any) -> dict[str, Any]:
        nonlocal active
        if call.function.name == "security_write":
            assert active == 0
            order.append("write")
        else:
            active += 1
            await asyncio.sleep(0)
            active -= 1
            order.append(call.id)
        return {"role": "tool", "tool_call_id": call.id, "content": "{}"}

    calls = [make_tool_call(str(i), "security_read", "{}") for i in range(6)]
    calls += [make_tool_call("write", "security_write", "{}")]
    calls += [make_tool_call("last", "security_read", "{}")]
    await _execute_tool_calls(calls, execute)
    assert order == [str(i) for i in range(6)] + ["write", "last"]


async def test_tool_batch_cancellation_leaves_no_active_executors() -> None:
    declare_local_tool("security_read", ToolEffect.READ)
    entered = asyncio.Event()
    active = 0

    async def execute(call: Any) -> dict[str, Any]:
        nonlocal active
        active += 1
        entered.set()
        try:
            await asyncio.Event().wait()
            return {}
        finally:
            active -= 1

    task = asyncio.create_task(
        _execute_tool_calls([make_tool_call("1", "security_read", "{}")], execute)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0
