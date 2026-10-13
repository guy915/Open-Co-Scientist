from __future__ import annotations

from typing import Any, cast

import pytest

from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.orchestration.checkpoint import (
    restore_workflow_state,
    serialize_workflow_state,
)
from co_scientist.orchestration.workflow_topology import route_after_meta_review
from co_scientist.science.meta_review.research_overview import is_interim_firing
from co_scientist.science.scheduling import (
    Budget,
    SchedulerStats,
    SupervisorDecision,
    TaskType,
    TerminationReason,
    decide_next_task,
    stacked_task_values,
    validate_decision,
)
from co_scientist.science.scheduling.policy import stack_companions
from tests._state import BUDGET, healthy_stats, make_state

_G, _E = TaskType.GENERATE, TaskType.EVOLVE


_REFLECT, _RANK = TaskType.REFLECT, TaskType.RANK


_CONTINUES = "continues"


_NOT_RANK = "not rank"


_R = TerminationReason


_OWED_COVERAGE = {
    "rankable_count": 3,
    "unmatched_rankable_count": 1,
    "owed_coverage_rounds": 1,
}


_EXHAUSTED = {"llm_calls": 1000}


def _budget(**limits: Any) -> Budget:
    return Budget(max_iterations=100, **limits)


# (stats overrides, budget, decide_next_task kwargs, expected outcome)
_DECISIONS: dict[str, tuple[dict[str, Any], Budget, dict[str, Any], Any]] = {
    "generation_heavy_generates": (
        {"generation_yield": 0.9, "evolution_yield": 0.1},
        BUDGET,
        {},
        _G,
    ),
    "evolution_heavy_evolves": (
        {"generation_yield": 0.1, "evolution_yield": 0.8},
        BUDGET,
        {},
        _E,
    ),
    "review_backlog_reflects": ({"unreviewed_count": 3}, BUDGET, {}, _REFLECT),
    "small_pool_generates_despite_evolution_yield": (
        {"pool_size": 1, "reviewed_count": 1, "evolution_yield": 0.9},
        BUDGET,
        {},
        _G,
    ),
    "low_coverage_ranks": (
        {"match_coverage": 0.0},
        BUDGET,
        {"min_match_coverage": 1.0},
        _RANK,
    ),
    "pool_growth_refreshes_proximity": (
        {"pool_grew_since_proximity": True},
        BUDGET,
        {},
        TaskType.PROXIMITY,
    ),
    "failed_task_is_retried": (
        {"last_task_failed": _RANK, "retries_remaining": 1},
        BUDGET,
        {},
        _RANK,
    ),
    "yield_tie_without_stagnation_generates": (
        {
            "generation_yield": 0.0,
            "evolution_yield": 0.0,
            "last_work_task": _E,
        },
        BUDGET,
        {},
        _G,
    ),
    "yield_tie_with_stagnation_evolves": (
        {
            "generation_yield": 0.0,
            "evolution_yield": 0.0,
            "rank_stable_cycles": 1,
            "last_work_task": _G,
        },
        BUDGET,
        {},
        _E,
    ),
    # Repeated breeding of a narrow pool would starve generation.
    "yield_tie_with_standing_stagnation_generates": (
        {
            "generation_yield": 0.0,
            "evolution_yield": 0.0,
            "rank_stable_cycles": 3,
            "last_work_task": _E,
        },
        BUDGET,
        {},
        _G,
    ),
    "stale_cancel_flag_is_the_executors_business": (
        {"cancelled": True},
        BUDGET,
        {},
        _CONTINUES,
    ),
    "llm_budget_terminates": (
        {"llm_calls": 50},
        _budget(max_llm_calls=50),
        {},
        _R.BUDGET,
    ),
    "task_budget_terminates": (
        {"tasks_run": 20},
        _budget(max_tasks=20),
        {},
        _R.MAX_TASKS,
    ),
    "wall_clock_terminates": (
        {"elapsed_s": 61.0},
        _budget(max_wall_clock_s=60.0),
        {},
        _R.WALL_CLOCK,
    ),
    "max_ideas_terminates": (
        {"pool_size": 10},
        _budget(max_ideas=10),
        {},
        _R.MAX_IDEAS,
    ),
    # Stopping before the backlog would strand already generated ideas.
    "max_ideas_defers_to_the_review_backlog": (
        {"pool_size": 10, "unreviewed_count": 2},
        _budget(max_ideas=10),
        {},
        _REFLECT,
    ),
    "max_matches_per_idea_terminates": (
        {"rankable_count": 6, "match_coverage": 3.0},
        _budget(max_matches_per_idea=3.0),
        {},
        _R.MAX_MATCHES_PER_IDEA,
    ),
    "below_max_matches_per_idea_continues": (
        {"rankable_count": 6, "match_coverage": 2.0},
        _budget(max_matches_per_idea=3.0),
        {},
        _CONTINUES,
    ),
    "iteration_budget_completes": (
        {"iteration": 5},
        Budget(max_iterations=5),
        {},
        _R.COMPLETED,
    ),
    # Failed growth still consumes iterations, avoiding the recursion limit.
    "starved_generation_completes_at_the_iteration_budget": (
        {"pool_size": 1, "reviewed_count": 1, "iteration": 5},
        Budget(max_iterations=5),
        {},
        _R.COMPLETED,
    ),
    # Gated ideas cannot earn tournament coverage or hold its floor open.
    "unrankable_pool_does_not_loop_on_ranking": (
        {"rankable_count": 0, "match_coverage": 0.0},
        BUDGET,
        {"min_match_coverage": 1.0},
        _NOT_RANK,
    ),
    "unrankable_pool_completes_at_the_iteration_budget": (
        {"rankable_count": 0, "match_coverage": 0.0, "iteration": 5},
        Budget(max_iterations=5),
        {},
        _R.COMPLETED,
    ),
    "convergence_terminates": (
        {
            "rank_stable_cycles": 2,
            "iteration": 2,
            "evolved_since_stable": True,
        },
        BUDGET,
        {"convergence_cycles": 2},
        _R.CONVERGED,
    ),
    "convergence_waits_for_min_cycles": (
        {"rank_stable_cycles": 5, "iteration": 1},
        BUDGET,
        {"convergence_cycles": 2, "min_cycles_before_convergence": 2},
        _CONTINUES,
    ),
    "stagnation_evolves_before_it_terminates": (
        {"rank_stable_cycles": 2, "iteration": 2},
        Budget(max_iterations=4),
        {"convergence_cycles": 2},
        _E,
    ),
    # The iteration ceiling still bounds convergence's evolution opportunity.
    "the_extra_evolve_cycle_cannot_outlive_the_iteration_budget": (
        {"rank_stable_cycles": 5, "iteration": 4},
        Budget(max_iterations=4),
        {"convergence_cycles": 2},
        _R.COMPLETED,
    ),
    "spent_tournament_budget_stops_asking_to_rank": (
        {"match_coverage": 0.0, "tournament_rounds_remaining": 0},
        BUDGET,
        {"min_match_coverage": 1.0},
        _NOT_RANK,
    ),
    "remaining_tournament_budget_still_ranks": (
        {"match_coverage": 0.0, "tournament_rounds_remaining": 4},
        BUDGET,
        {"min_match_coverage": 1.0},
        _RANK,
    ),
    # Precedence: safety > steering > owed coverage > owed review > budget.
    "safety_block_terminates": (
        {"safety_blocked": True},
        BUDGET,
        {},
        _R.SAFETY,
    ),
    "safety_outranks_budget_and_backlog": (
        {"safety_blocked": True, "llm_calls": 100, "unreviewed_count": 5},
        Budget(max_iterations=100, max_llm_calls=1),
        {},
        _R.SAFETY,
    ),
    "budget_outranks_an_ordinary_backlog": (
        {"llm_calls": 10, "unreviewed_count": 5},
        _budget(max_llm_calls=10),
        {},
        _R.BUDGET,
    ),
    "steering_generates": (
        {"pending_steering": True, "evolution_yield": 0.9},
        BUDGET,
        {},
        _G,
    ),
    # Pending steering intentionally buys a cycle above spent budget ceilings.
    "steering_outranks_budget_exhaustion": (
        {"llm_calls": 10, "pending_steering": True},
        _budget(max_llm_calls=10),
        {},
        _G,
    ),
    "steering_outranks_owed_coverage": (
        {**_OWED_COVERAGE, **_EXHAUSTED, "pending_steering": True},
        BUDGET,
        {},
        _G,
    ),
    "steering_outranks_owed_review": (
        {"owed_review_count": 1, "tasks_run": 100, "pending_steering": True},
        BUDGET,
        {},
        _G,
    ),
    "uncompared_idea_ranks_despite_a_healthy_average": (
        {**_OWED_COVERAGE, "match_coverage": 1.33},
        BUDGET,
        {},
        _RANK,
    ),
    # The ceiling is a runaway backstop; the allowance bounds the overshoot.
    "owed_coverage_outranks_budget_termination": (
        {**_OWED_COVERAGE, **_EXHAUSTED},
        BUDGET,
        {},
        _RANK,
    ),
    "owed_coverage_outranks_max_matches_per_idea": (
        {**_OWED_COVERAGE, "match_coverage": 1.0},
        Budget(max_iterations=5, max_matches_per_idea=1.0),
        {},
        _RANK,
    ),
    "safety_outranks_owed_coverage": (
        {**_OWED_COVERAGE, "safety_blocked": True},
        BUDGET,
        {},
        _R.SAFETY,
    ),
    "a_spent_allowance_stops_overriding_the_budget": (
        {**_OWED_COVERAGE, **_EXHAUSTED, "settlement_allowance": 0},
        BUDGET,
        {},
        _R.BUDGET,
    ),
    # A round that did not shrink the backlog will not next time either.
    "a_stalled_settlement_stops_overriding_the_budget": (
        {
            "rankable_count": 3,
            "unmatched_rankable_count": 2,
            "owed_coverage_rounds": 2,
            "settlement_allowance": 5,
            "owed_at_last_settlement": 2,
            **_EXHAUSTED,
        },
        BUDGET,
        {},
        _R.BUDGET,
    ),
    "settlement_continues_while_the_backlog_shrinks": (
        {
            **_OWED_COVERAGE,
            "settlement_allowance": 5,
            "owed_at_last_settlement": 3,
            **_EXHAUSTED,
        },
        BUDGET,
        {},
        _RANK,
    ),
    "a_single_rankable_idea_never_settles": (
        {
            "pool_size": 1,
            "rankable_count": 1,
            "unmatched_rankable_count": 1,
            "owed_coverage_rounds": 1,
            **_EXHAUSTED,
        },
        BUDGET,
        {},
        _NOT_RANK,
    ),
    "a_fully_compared_pool_is_unaffected": (
        {
            "rankable_count": 3,
            "unmatched_rankable_count": 0,
            **_EXHAUSTED,
        },
        BUDGET,
        {},
        _R.BUDGET,
    ),
    "owed_review_outranks_task_budget_termination": (
        {"owed_review_count": 1, "tasks_run": 100},
        BUDGET,
        {},
        _REFLECT,
    ),
    # A spent physical-call ceiling cannot fund forced reflection.
    "owed_review_refuses_to_override_the_llm_ceiling": (
        {"owed_review_count": 1, **_EXHAUSTED},
        BUDGET,
        {},
        _R.BUDGET,
    ),
    # The permanent override marker must not be spent by a healthy cycle.
    "owed_review_is_inert_while_the_budget_has_room": (
        {"owed_review_count": 1},
        BUDGET,
        {},
        _G,
    ),
    "safety_outranks_owed_review": (
        {"owed_review_count": 1, "tasks_run": 100, "safety_blocked": True},
        BUDGET,
        {},
        _R.SAFETY,
    ),
    "owed_coverage_outranks_owed_review": (
        {**_OWED_COVERAGE, "owed_review_count": 1, "tasks_run": 100},
        BUDGET,
        {},
        _RANK,
    ),
}


@pytest.mark.parametrize("case", _DECISIONS)
def test_policy_decides_by_precedence(case: str) -> None:
    overrides, budget, kwargs, expected = _DECISIONS[case]
    decision = decide_next_task(healthy_stats(**overrides), budget, **kwargs)

    if expected == _CONTINUES:
        assert not decision.terminate
    elif expected == _NOT_RANK:
        assert decision.next_task is not _RANK
    elif isinstance(expected, TerminationReason):
        assert decision.terminate
        assert decision.termination_reason is expected
    else:
        assert not decision.terminate
        assert decision.next_task is expected


@pytest.mark.parametrize(
    ("proposed", "stats", "corrected_to"),
    [
        (_RANK, {"pool_size": 1, "rankable_count": 1}, _G),
        (_RANK, {"pending_steering": True}, _G),
        (_E, {"reviewed_count": 0, "unreviewed_count": 3}, _REFLECT),
        (
            _E,
            {"reviewed_count": 0, "unreviewed_count": 0, "pool_size": 2},
            _G,
        ),
    ],
)
def test_validation_corrects_an_unfit_proposal_and_keeps_queue_actions(
    proposed: TaskType, stats: dict[str, Any], corrected_to: TaskType
) -> None:
    """Failed rows have no other revival path, so corrections retain queue
    actions."""
    retry = ({"action": "retry", "task_id": "t-9", "reason": "transient"},)
    validated = validate_decision(
        SupervisorDecision(proposed, "llm said so", queue_actions=retry),
        healthy_stats(**stats),
    )
    assert validated.next_task is corrected_to
    assert "corrected" in validated.reason
    assert validated.queue_actions == retry


def _settled_stats(**overrides: object) -> SchedulerStats:
    base: dict[str, object] = {
        "pool_size": 6,
        "reviewed_count": 6,
        "unreviewed_count": 0,
        "rankable_count": 6,
        "total_matches": 12,
        "match_coverage": 2.0,
        "iteration": 1,
        "iterations_since_meta_review": 1,
        "feedback_since_meta_review": 6,
    }
    base.update(overrides)
    return SchedulerStats(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "fires"),
    [
        ({}, True),
        # No completed work cycle yet, or nothing new to synthesize: a second
        # pass would buy no new information.
        ({"iterations_since_meta_review": 0}, False),
        ({"feedback_since_meta_review": 0}, False),
        ({"unreviewed_count": 3}, False),
    ],
)
def test_meta_review_fires_once_new_critique_material_exists(
    overrides: dict[str, Any], fires: bool
) -> None:
    stats = _settled_stats(**overrides)
    decision = validate_decision(decide_next_task(stats, Budget(max_iterations=4)), stats)
    assert (decision.next_task is TaskType.META_REVIEW) is fires


_CHEAP_BUDGET = Budget(max_iterations=4, max_llm_calls=1200)


def _due_stats(**overrides: object) -> SchedulerStats:
    base: dict[str, object] = {
        "pool_size": 6,
        "reviewed_count": 6,
        "unreviewed_count": 2,
        "rankable_count": 6,
        "total_matches": 12,
        "match_coverage": 2.0,
        "iteration": 1,
        "iterations_since_meta_review": 1,
        "feedback_since_meta_review": 6,
    }
    base.update(overrides)
    return SchedulerStats(**base)  # type: ignore[arg-type]


def _overview_due(**overrides: object) -> SchedulerStats:
    return _due_stats(iterations_since_research_overview=2, **overrides)


def test_the_overview_companion_is_gated_by_the_tier_ceiling() -> None:
    stats = _overview_due()
    decision = stack_companions(decide_next_task(stats, _CHEAP_BUDGET), stats, _CHEAP_BUDGET)
    assert stacked_task_values(decision.queue_actions) == (TaskType.META_REVIEW.value,)


def test_the_stacked_list_survives_a_checkpoint_round_trip() -> None:
    """Companion tasks read restored state; losing this key silently skips or
    mislabels synthesis."""
    state = make_state(next_task=TaskType.REFLECT.value)
    state["supervisor_queue_actions"] = [
        {"action": "enqueue", "task_type": TaskType.META_REVIEW.value},
        {"action": "enqueue", "task_type": TaskType.SYNTHESIZE.value},
    ]
    restored = restore_workflow_state(serialize_workflow_state(dict(state), last_event_seq=0))
    assert stacked_task_values(restored["supervisor_queue_actions"]) == (
        TaskType.META_REVIEW.value,
        TaskType.SYNTHESIZE.value,
    )
    assert is_interim_firing(cast(WorkflowState, restored))
    assert route_after_meta_review(cast(WorkflowState, restored)) == ("research_overview")


def test_a_terminating_decision_is_never_wrapped() -> None:
    stats = _due_stats(unreviewed_count=0, llm_calls=99)
    spent = Budget(max_iterations=4, max_llm_calls=1)
    decision = stack_companions(decide_next_task(stats, spent), stats, spent)
    assert decision.terminate
    assert not stacked_task_values(decision.queue_actions)
