from __future__ import annotations

from typing import Any

import pytest

from co_scientist.domains.research_state.models import ExecutionMetrics, Hypothesis
from co_scientist.orchestration.engine_tasks.fanout import _mature_reflection_specs
from co_scientist.orchestration.engine_tasks.gate import _gated_hypotheses
from co_scientist.orchestration.workflow_topology import (
    route_after_deep_verification,
    route_next_task,
)
from co_scientist.science.reflection import deep_verification as dv
from co_scientist.science.scheduling.funnel import finalists
from tests._llm_fake import mock_call_llm_json
from tests._state import make_hypothesis, make_review, make_state, make_verification_response


def _idea(text: str, elo: int = 1200, played: int = 0, **overrides: Any) -> Hypothesis:
    fields: dict[str, Any] = {"review_disposition": "viable", **overrides}
    return make_hypothesis(
        text=text, reviews=[make_review()], elo_rating=elo, win_count=played, **fields
    )


def _ranked_pool() -> list[Hypothesis]:
    return [
        _idea("leader", 1290, played=3),
        _idea("runner-up", 1240, played=3),
        _idea("trailer", 1150, played=3),
        _idea("unplayed child", 1200),
    ]


def test_no_idea_is_a_finalist_before_its_first_match() -> None:
    pool = [_idea(f"fresh {i}") for i in range(4)]

    assert finalists(make_state(hypotheses=pool)) == []


def test_finalists_are_the_ranked_leaders_up_to_the_tier_count() -> None:
    state = make_state(hypotheses=_ranked_pool(), budget={"finalists": 2})

    assert [h.text for h in finalists(state)] == ["leader", "runner-up"]


def test_a_pool_too_small_to_rank_keeps_its_idea_as_finalist() -> None:
    lone = _idea("only reviewed idea")
    unreviewed = make_hypothesis(text="awaiting review")

    assert finalists(make_state(hypotheses=[lone, unreviewed])) == [lone]


async def test_deep_verification_skips_ideas_that_are_not_finalists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mock_call_llm_json(monkeypatch, dv, make_verification_response())
    pool = _ranked_pool()

    await dv.deep_verification_node(make_state(hypotheses=pool, budget={"finalists": 2}))

    assert fake.await_count == 2
    assert [h.text for h in pool if h.deep_verification_verdict] == ["leader", "runner-up"]


def test_comprehensive_reflection_queues_depth_for_finalists_only() -> None:
    state = make_state(hypotheses=_ranked_pool(), budget={"finalists": 2})

    specs = _mature_reflection_specs(dict(state))

    leader, runner_up = state["hypotheses"][:2]
    assert {spec.hypothesis_id for spec in specs} == {leader.id, runner_up.id}
    assert {spec.review_mode for spec in specs} == {"finalist"}


def test_a_blocked_idea_keeps_its_one_recheck_outside_the_finalists() -> None:
    blocked = _idea("blocked", review_disposition="inaccurate")
    state = make_state(hypotheses=[*_ranked_pool(), blocked], budget={"finalists": 1})

    specs = _mature_reflection_specs(dict(state))

    assert [
        (spec.review_mode, spec.recheck) for spec in specs if spec.hypothesis_id == blocked.id
    ] == [("recurrent", True)]


def _terminal_state(**overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "hypotheses": _ranked_pool(),
        "budget": {"finalists": 2, "max_llm_calls": 1200},
        "next_task": "terminate",
        "termination_reason": "completed",
        "supervisor_queue_actions": [],
        "metrics": ExecutionMetrics(llm_calls=100),
    }
    fields.update(overrides)
    return make_state(**fields)


def test_finalists_without_depth_get_it_before_the_terminal_overview() -> None:
    assert route_next_task(_terminal_state()) == "comprehensive_reflection"


def test_finalists_with_depth_go_straight_to_the_overview() -> None:
    state = _terminal_state()
    for hypothesis in state["hypotheses"][:2]:
        hypothesis.deep_verification_verdict = "holds"

    assert route_next_task(state) == "research_overview"


@pytest.mark.parametrize("reason", ["budget", "wall_clock", "max_tasks", "safety", None])
def test_a_hard_stop_never_buys_terminal_depth(reason: str | None) -> None:
    assert route_next_task(_terminal_state(termination_reason=reason)) == "research_overview"


def test_terminal_depth_never_spends_past_the_call_ceiling() -> None:
    state = _terminal_state(metrics=ExecutionMetrics(llm_calls=1190))

    assert route_next_task(state) == "research_overview"


def test_the_terminal_pass_reports_after_verification_and_every_other_pass_ranks() -> None:
    assert route_after_deep_verification(_terminal_state()) == "research_overview"
    assert route_after_deep_verification(_terminal_state(next_task="evolve")) == "ranking"
    assert route_after_deep_verification(make_state()) == "ranking"


def test_the_terminal_pass_fills_missing_depth_without_refreshing_or_rechecking() -> None:
    blocked = _idea("blocked", review_disposition="inaccurate")
    state = _terminal_state(hypotheses=[*_ranked_pool(), blocked], current_iteration=3)
    leader, runner_up = state["hypotheses"][:2]
    # A later cycle would refresh this full review; the terminal pass does not.
    leader.enrichments["full"] = {"verdict": "sound"}

    specs = _mature_reflection_specs(dict(state))

    assert [(spec.hypothesis_id, spec.review_mode) for spec in specs] == [
        (runner_up.id, "finalist")
    ]


def test_the_claim_gate_checks_finalists_and_reassesses_past_blocks() -> None:
    pool = _ranked_pool()
    blocked = _idea("evidence blocked", review_disposition="evidence_blocked")
    state: dict[str, Any] = dict(make_state(hypotheses=[*pool, blocked], budget={"finalists": 1}))

    assert [h.text for h in _gated_hypotheses(state)] == ["leader", "evidence blocked"]


def test_finalize_checks_claims_only_for_examined_ideas() -> None:
    from co_scientist.orchestration.drain import FinalStateInputs, _depth_reviewed_candidates

    inputs = FinalStateInputs(
        hyps_parents_first=[
            {"id": "engine-finalist", "deep_verification_verdict": "weakened"},
            {"id": "engine-screened", "deep_verification_verdict": None},
        ],
        articles=[],
        matchups=[],
        proximity_graph={},
        persisted_engine_ids={"engine-finalist", "engine-screened"},
        final_state={},
    )
    store_ids = {"engine-finalist": "finalist", "engine-screened": "screened"}

    candidates = [{"id": "finalist"}, {"id": "screened"}]

    assert _depth_reviewed_candidates(candidates, inputs, store_ids) == [{"id": "finalist"}]
