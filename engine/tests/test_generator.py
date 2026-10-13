from __future__ import annotations

from typing import Any

import pytest

from co_scientist.orchestration.generator.core import HypothesisGenerator
from co_scientist.orchestration.generator.run_setup import GeneratorOptions
from co_scientist.orchestration.workflow_topology import TASK_ROUTES
from co_scientist.platform.llm.offline.llm import DEFAULT_OFFLINE_MODEL
from co_scientist.platform.retrieval import mcp_client
from co_scientist.science.scheduling import ALLOWED_LOOP_TASKS
from tests._mcp import stub_mcp_availability


def test_task_routes_reconcile_with_allowed_loop_tasks() -> None:
    assert {task.value for task in ALLOWED_LOOP_TASKS} == set(TASK_ROUTES)


async def test_availability_is_probed_once_per_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[Any] = []

    async def _probe(**kwargs: Any) -> bool:
        probes.append(kwargs.get("tool_registry"))
        return True

    monkeypatch.setattr(mcp_client, "check_mcp_available", _probe)
    monkeypatch.setattr(mcp_client, "check_literature_source_available", _probe)

    generator = HypothesisGenerator()
    await generator.prepare_task_state("goal one")
    await generator.prepare_task_state("goal two")
    assert len(probes) == 2, "one concurrent probe pair, cached afterward"

    configured_generator = HypothesisGenerator(options=GeneratorOptions(disable_tools=["pubmed"]))
    await configured_generator.prepare_task_state("goal three")
    assert len(probes) == 4, "a registry change must re-probe"


@pytest.mark.parametrize(
    ("available", "opt", "model", "expected"),
    [
        (True, None, None, False),  # opt-in: transcripts multiply cost
        (True, True, None, True),
        (True, False, None, False),
        (False, True, None, False),
        # The offline responder emits no tool calls.
        (True, True, DEFAULT_OFFLINE_MODEL, False),
    ],
)
async def test_tool_calling_generation_requires_opt_in_and_capability(
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    opt: bool | None,
    model: str | None,
    expected: bool,
) -> None:
    stub_mcp_availability(monkeypatch, available=available)
    gen = HypothesisGenerator(model_name=model) if model else HypothesisGenerator()
    opts = {} if opt is None else {"enable_tool_calling_generation": opt}
    state = await gen.prepare_task_state("goal", opts=opts)
    assert state["enable_tool_calling_generation"] is expected
