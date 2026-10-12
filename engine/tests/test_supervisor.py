from __future__ import annotations

from typing import Any

import pytest

from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science.scheduling import (
    Budget,
    SchedulerStats,
    TaskType,
)
from co_scientist.science.supervisor import supervisor_decision
from tests._llm_fake import mock_call_llm_json, stub_call_llm_json
from tests._state import make_allocation_response, make_state


def _supervisor_decision_state() -> WorkflowState:
    return make_state(
        research_goal="Find a testable mechanism.",
        supervisor_model_name="test/model",
        run_id="run-1",
        supervisor_guidance={"workflow_plan": {}},
        task_history=[],
        meta_review={},
        pending_steering=False,
        held_for_review=[],
    )


def _stub_allocation(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> None:
    stub_call_llm_json(monkeypatch, supervisor_decision, response)


def _forbid_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_call_llm_json(
        monkeypatch,
        supervisor_decision,
        side_effect=AssertionError("model must not be called"),
    )


@pytest.mark.asyncio
async def test_hard_budget_stop_bypasses_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    _forbid_allocation(monkeypatch)
    stats = SchedulerStats(pool_size=4, reviewed_count=4, iteration=1, llm_calls=10)
    (
        decision,
        provenance,
        _,
    ) = await supervisor_decision.choose_supervisor_task(
        _supervisor_decision_state(),
        stats,
        Budget(max_iterations=4, max_llm_calls=10),
    )
    assert decision.terminate
    assert provenance == "hard-invariant"


@pytest.mark.asyncio
async def test_iteration_budget_blocks_model_directed_pool_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    _stub_allocation(
        monkeypatch,
        make_allocation_response("evolve", "Keep expanding the pool."),
    )
    stats = SchedulerStats(
        pool_size=8,
        reviewed_count=7,
        unreviewed_count=1,
        iteration=2,
    )

    (
        decision,
        provenance,
        _,
    ) = await supervisor_decision.choose_supervisor_task(
        _supervisor_decision_state(), stats, Budget(max_iterations=2)
    )

    assert decision.next_task is TaskType.REFLECT
    assert provenance == "required-transition"


@pytest.mark.asyncio
async def test_repeated_maintenance_cannot_stall_iteration_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    _stub_allocation(
        monkeypatch,
        make_allocation_response("reflect", "Run reflection again without new work."),
    )
    state = _supervisor_decision_state()
    state["task_history"] = [
        {
            "task_type": "reflect",
            "status": "queued",
            "reason": "First reflection pass.",
            "iteration": 0,
        }
    ]
    stats = SchedulerStats(
        pool_size=8,
        reviewed_count=8,
        unreviewed_count=0,
        total_matches=16,
        match_coverage=2.0,
        iteration=0,
        last_work_task=TaskType.GENERATE,
    )

    (
        decision,
        provenance,
        _,
    ) = await supervisor_decision.choose_supervisor_task(state, stats, Budget(max_iterations=2))

    assert decision.next_task is TaskType.GENERATE
    assert provenance == "hard-invariant"


_RETRY = {
    "action": "retry",
    "task_id": "task-9",
    "reason": "Transient provider failure.",
}


def _supervisor_decision_guards_state() -> WorkflowState:
    return make_state(
        research_goal="Find a testable mechanism.",
        supervisor_model_name="test/model",
        run_id="run-1",
        supervisor_guidance={"workflow_plan": {}},
        task_history=[],
        meta_review={},
        pending_steering=False,
        held_for_review=[],
        durable_task_queue=[
            {
                "task_id": "task-9",
                "task_type": "engine.fanout.review.item",
                "status": "failed",
                "error": "provider unavailable",
            }
        ],
    )


def _allocating(task: str) -> Any:

    async def _allocation(**_kwargs: Any) -> dict[str, Any]:
        return make_allocation_response(
            task,
            "Revive the failed row and keep working.",
            queue_actions=[dict(_RETRY)],
        )

    return _allocation


@pytest.mark.asyncio
async def test_post_budget_growth_guard_still_delivers_the_queue_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal-drain guard holds forever; dropping revival there strands
    the row permanently."""
    monkeypatch.setattr(supervisor_decision, "call_llm_json", _allocating("generate"))
    stats = SchedulerStats(
        pool_size=8,
        reviewed_count=6,
        unreviewed_count=2,
        rankable_count=8,
        iteration=2,
    )

    decision, provenance, _ = await supervisor_decision.choose_supervisor_task(
        _supervisor_decision_guards_state(), stats, Budget(max_iterations=2)
    )

    assert decision.next_task is not TaskType.GENERATE
    assert provenance == "hard-invariant"
    assert decision.queue_actions == (_RETRY,)
