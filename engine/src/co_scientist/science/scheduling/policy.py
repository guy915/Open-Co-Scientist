"""The model advises; deterministic checks enforce budgets and transitions.
Check order defines their precedence."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Final

from co_scientist.science.scheduling.models import (
    ENQUEUE_ACTION,
    Budget,
    SchedulerStats,
    SupervisorDecision,
    TaskType,
    TerminationReason,
    stacked_task_values,
)


def _terminate(reason: TerminationReason, message: str) -> SupervisorDecision:
    return SupervisorDecision(
        next_task=TaskType.TERMINATE,
        reason=message,
        terminate=True,
        termination_reason=reason,
    )


def _llm_call_budget_check(stats: SchedulerStats, budget: Budget) -> SupervisorDecision | None:
    max_calls = budget.max_llm_calls
    if max_calls is None or stats.llm_calls < max_calls:
        return None
    return _terminate(
        TerminationReason.BUDGET,
        f"LLM-call budget exhausted ({stats.llm_calls}/{max_calls})",
    )


def _task_budget_check(stats: SchedulerStats, budget: Budget) -> SupervisorDecision | None:
    max_tasks = budget.max_tasks
    if max_tasks is None or stats.tasks_run < max_tasks:
        return None
    return _terminate(
        TerminationReason.MAX_TASKS,
        f"task budget exhausted ({stats.tasks_run}/{max_tasks})",
    )


def _wall_clock_budget_check(stats: SchedulerStats, budget: Budget) -> SupervisorDecision | None:
    limit = budget.max_wall_clock_s
    if limit is None or stats.elapsed_s < limit:
        return None
    return _terminate(
        TerminationReason.WALL_CLOCK,
        f"wall-clock budget exhausted ({stats.elapsed_s:.0f}s/{limit:.0f}s)",
    )


def _max_ideas_check(stats: SchedulerStats, budget: Budget) -> SupervisorDecision | None:
    """Budget checks precede review backlog; do not strand freshly admitted
    ideas unreviewed."""
    limit = budget.max_ideas
    if limit is None or stats.unreviewed_count > 0 or stats.pool_size < limit:
        return None
    return _terminate(
        TerminationReason.MAX_IDEAS,
        f"idea-pool budget exhausted ({stats.pool_size}/{limit})",
    )


def _max_matches_per_idea_check(stats: SchedulerStats, budget: Budget) -> SupervisorDecision | None:
    """The match ceiling shares average rankable coverage semantics with the
    tournament floor."""
    limit = budget.max_matches_per_idea
    if limit is None or stats.rankable_count < 2 or stats.match_coverage < limit:
        return None
    return _terminate(
        TerminationReason.MAX_MATCHES_PER_IDEA,
        f"match budget exhausted (avg {stats.match_coverage:.2f}/{limit:.2f} matches per idea)",
    )


def _budget_termination(stats: SchedulerStats, budget: Budget) -> SupervisorDecision | None:
    for check in (
        _llm_call_budget_check,
        _task_budget_check,
        _wall_clock_budget_check,
        _max_ideas_check,
        _max_matches_per_idea_check,
    ):
        decision = check(stats, budget)
        if decision is not None:
            return decision
    return None


def _check_owed_review(stats: SchedulerStats, budget: Budget) -> SupervisorDecision | None:
    """Spend each idea's bounded review override only at exhaustion; crossing
    the in-task LLM ceiling causes permanent failure."""
    termination = _budget_termination(stats, budget)
    if termination is None:
        return None
    if termination.termination_reason is TerminationReason.BUDGET:
        return None
    if stats.owed_review_count < 1:
        return None
    return SupervisorDecision(
        next_task=TaskType.REFLECT,
        reason=(
            f"{stats.owed_review_count} hypothesis(es) admitted but never "
            "reviewed; force one review pass before the budget can "
            "terminate the run"
        ),
    )


RESEARCH_OVERVIEW_MIN_LLM_CALLS: Final = 7000
"""The extended-tier ceiling funds this large, retryable prompt; lower tiers
cannot justify its cost."""

RESEARCH_OVERVIEW_CADENCE_CYCLES: Final = 2
"""Wait for a changed generation/ranking cycle; an overview of an unchanged
pool gives generation no new guidance."""


def _check_meta_review_cadence(
    stats: SchedulerStats,
) -> SupervisorDecision | None:
    """Meta-review is not a work task; require both a completed cycle and new
    feedback so it cannot loop on unchanged material."""
    if not stats.meta_review_enabled:
        return None
    if stats.iterations_since_meta_review < 1:
        return None
    if stats.feedback_since_meta_review < 1:
        return None
    return SupervisorDecision(
        next_task=TaskType.META_REVIEW,
        reason=(
            f"{stats.feedback_since_meta_review} new review(s)/match(es) "
            f"over {stats.iterations_since_meta_review} cycle(s) since the "
            "last system-wide feedback; synthesize it"
        ),
    )


def _check_research_overview_cadence(
    stats: SchedulerStats, budget: Budget
) -> SupervisorDecision | None:
    """This large, retryable prompt needs the extended-tier budget and a changed
    work cycle; scheduling resets its anchor because synthesis is not work."""
    ceiling = budget.max_llm_calls
    if ceiling is None or ceiling < RESEARCH_OVERVIEW_MIN_LLM_CALLS:
        return None
    if stats.iterations_since_research_overview < (RESEARCH_OVERVIEW_CADENCE_CYCLES):
        return None
    return SupervisorDecision(
        next_task=TaskType.SYNTHESIZE,
        reason=(
            f"{stats.iterations_since_research_overview} work cycle(s) "
            "since the last research overview; synthesize an interim one "
            "for the next generation cycle"
        ),
    )


def _generate(reason: str) -> SupervisorDecision:
    return SupervisorDecision(next_task=TaskType.GENERATE, reason=reason)


def _generation_vs_evolution(stats: SchedulerStats) -> SupervisorDecision:
    gen_y, evo_y = stats.generation_yield, stats.evolution_yield
    can_evolve = stats.reviewed_count >= 2
    if evo_y > gen_y and can_evolve:
        return SupervisorDecision(
            next_task=TaskType.EVOLVE,
            reason=(
                f"evolution out-yields generation (evo={evo_y:.2f} > gen={gen_y:.2f}); "
                "evolve leaders"
            ),
        )
    if gen_y > evo_y:
        return _generate(
            f"generation out-yields evolution (gen={gen_y:.2f} > "
            f"evo={evo_y:.2f}); generate new regions"
        )
    return _tie_break(stats, can_evolve)


def _tie_break(stats: SchedulerStats, can_evolve: bool) -> SupervisorDecision:
    """Persistent stagnation must not repeatedly breed near-identical
    descendants; let evolution try once before exploring a new region."""
    just_evolved = stats.last_work_task is TaskType.EVOLVE
    if can_evolve and stats.rank_stable_cycles >= 1 and not just_evolved:
        return SupervisorDecision(
            next_task=TaskType.EVOLVE,
            reason=(
                f"yield tie (gen=evo={stats.generation_yield:.2f}) on the "
                f"transition into a stagnant leaderboard "
                f"({stats.rank_stable_cycles} stable cycle(s)); evolve leaders"
            ),
        )
    if stats.rank_stable_cycles >= 1:
        return _generate(
            f"yield tie (gen=evo={stats.generation_yield:.2f}) with a "
            "leaderboard still stagnant after evolution already had its "
            "turn; generate new regions instead of re-breeding it"
        )
    return _generate(
        f"yield tie (gen=evo={stats.generation_yield:.2f}) with no measured "
        "leaderboard stagnation; generate new regions"
    )


def _check_stop_signals(stats: SchedulerStats) -> SupervisorDecision | None:
    """Cancellation is enforced by durable dispatch, not a second scheduler
    signal."""
    if stats.safety_blocked:
        return _terminate(TerminationReason.SAFETY, "safety block halted the run")
    return None


def _check_owed_coverage(
    stats: SchedulerStats,
) -> SupervisorDecision | None:
    """Open, size and close settlement on the same owed rounds; a decreasing
    allowance bounds it even when ranking stalls."""
    if stats.owed_coverage_rounds < 1:
        return None

    if stats.rankable_count < 2:
        return None
    allowance = stats.settlement_allowance
    if allowance is not None and allowance < 1:
        return None
    previous = stats.owed_at_last_settlement
    if previous is not None and stats.owed_coverage_rounds >= previous:
        return None
    return SupervisorDecision(
        next_task=TaskType.RANK,
        reason=(
            f"{stats.owed_coverage_rounds} tournament round(s) owed to bring "
            "every rankable idea to minimum coverage "
            f"({stats.unmatched_rankable_count} have never been matched); "
            "settle coverage before terminating"
        ),
    )


def _check_retry(stats: SchedulerStats) -> SupervisorDecision | None:
    if stats.last_task_failed is not None and stats.retries_remaining > 0:
        return SupervisorDecision(
            next_task=stats.last_task_failed,
            reason=(
                f"retrying failed task {stats.last_task_failed.value} "
                f"({stats.retries_remaining} retries remaining)"
            ),
        )
    return None


def _check_steering(stats: SchedulerStats) -> SupervisorDecision | None:
    """Observation consumes steering; it must outrank settlement and ceilings
    or its one-shot instruction is marked applied without incorporation."""
    if stats.pending_steering:
        return SupervisorDecision(
            next_task=TaskType.GENERATE,
            reason="pending user steering; generate to incorporate it",
            priority=100,
        )
    return None


def _check_review_backlog(
    stats: SchedulerStats,
) -> SupervisorDecision | None:
    if stats.unreviewed_count > 0:
        return SupervisorDecision(
            next_task=TaskType.REFLECT,
            reason=(f"{stats.unreviewed_count} unreviewed hypotheses; review before ranking"),
        )
    return None


def _check_pool_size(stats: SchedulerStats, budget: Budget) -> SupervisorDecision | None:
    """The iteration ceiling must still stop generation when a pool never
    grows enough to rank."""
    if stats.pool_size >= 2:
        return None
    if stats.iteration >= budget.max_iterations:
        return _terminate(
            TerminationReason.COMPLETED,
            f"pool too small to continue ({stats.pool_size}) and "
            f"iteration budget reached ({stats.iteration}/"
            f"{budget.max_iterations})",
        )
    return SupervisorDecision(
        next_task=TaskType.GENERATE,
        reason=(f"pool too small for a tournament ({stats.pool_size}); generate more"),
    )


def _check_tournament_coverage(
    stats: SchedulerStats, min_match_coverage: float
) -> SupervisorDecision | None:
    """Impossible or budget-depleted coverage must not loop on ranking tasks
    that do no work."""
    remaining = stats.tournament_rounds_remaining
    if remaining is not None and remaining < 1:
        return None
    if stats.rankable_count >= 2 and stats.match_coverage < min_match_coverage:
        return SupervisorDecision(
            next_task=TaskType.RANK,
            reason=(
                f"avg match coverage {stats.match_coverage:.2f} below "
                f"{min_match_coverage:.2f}; run more tournament rounds"
            ),
        )
    return None


def _check_proximity_refresh(
    stats: SchedulerStats,
) -> SupervisorDecision | None:
    if stats.pool_grew_since_proximity:
        return SupervisorDecision(
            next_task=TaskType.PROXIMITY,
            reason="pool grew since last proximity; refresh clustering",
        )
    return None


def _check_convergence(
    stats: SchedulerStats,
    convergence_cycles: int,
    min_cycles_before_convergence: int,
) -> SupervisorDecision | None:
    """A stagnant pool earns an evolution attempt; max_iterations still
    bounds the wait."""
    if not stats.evolved_since_stable:
        return None
    if (
        stats.iteration >= min_cycles_before_convergence
        and stats.rank_stable_cycles >= convergence_cycles
    ):
        return _terminate(
            TerminationReason.CONVERGED,
            f"top-ranked Elo stable for {stats.rank_stable_cycles} cycles",
        )
    return None


def _check_iteration_budget(stats: SchedulerStats, budget: Budget) -> SupervisorDecision | None:
    if stats.iteration >= budget.max_iterations:
        return _terminate(
            TerminationReason.COMPLETED,
            f"reached iteration budget ({stats.iteration}/{budget.max_iterations})",
        )
    return None


# Periodic overview returns to the loop; independent meta-review also serves
# runs that never evolve, so neither may be rewritten as another task.
ALLOWED_LOOP_TASKS: frozenset[TaskType] = frozenset(
    {
        TaskType.GENERATE,
        TaskType.REFLECT,
        TaskType.RANK,
        TaskType.EVOLVE,
        TaskType.META_REVIEW,
        TaskType.PROXIMITY,
        TaskType.SYNTHESIZE,
        TaskType.TERMINATE,
    }
)


def _correct_for_steering(task: TaskType, stats: SchedulerStats) -> SupervisorDecision | None:
    if stats.pending_steering and task is not TaskType.GENERATE:
        return SupervisorDecision(
            next_task=TaskType.GENERATE,
            reason=("corrected: scientist steering reprioritized fresh generation"),
            priority=100,
        )
    return None


def _correct_disallowed_task(task: TaskType, stats: SchedulerStats) -> SupervisorDecision | None:
    if task not in ALLOWED_LOOP_TASKS:
        return SupervisorDecision(
            next_task=TaskType.GENERATE,
            reason=f"corrected: {task.value} is not a dispatchable loop task",
        )
    return None


def _correct_rank_precondition(task: TaskType, stats: SchedulerStats) -> SupervisorDecision | None:
    if task == TaskType.RANK and stats.rankable_count < 2:
        return SupervisorDecision(
            next_task=TaskType.GENERATE,
            reason=(
                "corrected: fewer than two rankable hypotheses "
                f"({stats.rankable_count}); generate instead of looping on rank"
            ),
        )
    return None


def _correct_evolve_precondition(
    task: TaskType, stats: SchedulerStats
) -> SupervisorDecision | None:
    if task == TaskType.EVOLVE and stats.reviewed_count < 1:
        fallback = TaskType.REFLECT if stats.unreviewed_count > 0 else TaskType.GENERATE
        return SupervisorDecision(
            next_task=fallback,
            reason=(f"corrected: cannot evolve with no reviewed hypotheses; {fallback.value}"),
        )
    return None


def validate_decision(decision: SupervisorDecision, stats: SchedulerStats) -> SupervisorDecision:
    """Corrections must retain queue actions: failed durable rows are not
    automatically revived, so discarding revival can loop."""
    if decision.terminate:
        return decision

    task = decision.next_task
    for correct in (
        _correct_for_steering,
        _correct_disallowed_task,
        _correct_rank_precondition,
        _correct_evolve_precondition,
    ):
        corrected = correct(task, stats)
        if corrected is not None:
            return dataclasses.replace(corrected, queue_actions=decision.queue_actions)
    return decision


__all__ = [
    "ALLOWED_LOOP_TASKS",
    "CONVERGENCE_CYCLES_DEFAULT",
    "MIN_CYCLES_BEFORE_CONVERGENCE_DEFAULT",
    "MIN_MATCH_COVERAGE_DEFAULT",
    "Budget",
    "SchedulerStats",
    "SupervisorDecision",
    "TaskType",
    "TerminationReason",
    "_budget_termination",
    "_check_convergence",
    "_check_iteration_budget",
    "_check_meta_review_cadence",
    "_check_owed_coverage",
    "_check_owed_review",
    "_check_pool_size",
    "_check_proximity_refresh",
    "_check_research_overview_cadence",
    "_check_retry",
    "_check_review_backlog",
    "_check_steering",
    "_check_stop_signals",
    "_check_tournament_coverage",
    "_correct_disallowed_task",
    "_correct_evolve_precondition",
    "_correct_for_steering",
    "_correct_rank_precondition",
    "_generate",
    "_generation_vs_evolution",
    "_ordered_checks",
    "_terminate",
    "decide_next_task",
    "required_transition",
    "stack_companions",
    "stacked_task_values",
    "validate_decision",
]


MIN_MATCH_COVERAGE_DEFAULT = 1.0


CONVERGENCE_CYCLES_DEFAULT = 2


# Local defaults allow both exploration modes before early convergence.
MIN_CYCLES_BEFORE_CONVERGENCE_DEFAULT = 2


def _ordered_checks(
    stats: SchedulerStats,
    budget: Budget,
    min_match_coverage: float,
    convergence_cycles: int,
    min_cycles_before_convergence: int,
) -> tuple[Callable[[], SupervisorDecision | None], ...]:
    return (
        lambda: _check_stop_signals(stats),
        lambda: _check_steering(stats),
        lambda: _check_owed_coverage(stats),
        lambda: _check_owed_review(stats, budget),
        lambda: _budget_termination(stats, budget),
        lambda: _check_retry(stats),
        lambda: _check_review_backlog(stats),
        lambda: _check_pool_size(stats, budget),
        lambda: _check_tournament_coverage(stats, min_match_coverage),
        lambda: _check_proximity_refresh(stats),
        lambda: _check_meta_review_cadence(stats),
        lambda: _check_convergence(stats, convergence_cycles, min_cycles_before_convergence),
        lambda: _check_iteration_budget(stats, budget),
        # Terminal report consumes critique; interim overview needs a future
        # generation reader, so schedule it only below termination checks.
        lambda: _check_research_overview_cadence(stats, budget),
    )


def required_transition(
    stats: SchedulerStats,
    budget: Budget,
    *,
    min_match_coverage: float = MIN_MATCH_COVERAGE_DEFAULT,
    convergence_cycles: int = CONVERGENCE_CYCLES_DEFAULT,
    min_cycles_before_convergence: int = MIN_CYCLES_BEFORE_CONVERGENCE_DEFAULT,
) -> SupervisorDecision | None:
    """Forced transitions need no model round-trip; reserve planning for the
    open choice."""
    for check in _ordered_checks(
        stats,
        budget,
        min_match_coverage,
        convergence_cycles,
        min_cycles_before_convergence,
    ):
        decision = check()
        if decision is not None:
            return decision
    return None


def decide_next_task(
    stats: SchedulerStats,
    budget: Budget,
    *,
    min_match_coverage: float = MIN_MATCH_COVERAGE_DEFAULT,
    convergence_cycles: int = CONVERGENCE_CYCLES_DEFAULT,
    min_cycles_before_convergence: int = MIN_CYCLES_BEFORE_CONVERGENCE_DEFAULT,
) -> SupervisorDecision:
    forced = required_transition(
        stats,
        budget,
        min_match_coverage=min_match_coverage,
        convergence_cycles=convergence_cycles,
        min_cycles_before_convergence=min_cycles_before_convergence,
    )
    if forced is not None:
        return forced

    return _generation_vs_evolution(stats)


_UNSTACKABLE_TASKS = frozenset({TaskType.TERMINATE})


# Avoid duplicate shared-node execution and overview-before-critique inversion.
_COMPANION_CONFLICTS: dict[TaskType, frozenset[TaskType]] = {
    TaskType.META_REVIEW: frozenset({TaskType.META_REVIEW, TaskType.EVOLVE}),
    TaskType.SYNTHESIZE: frozenset({TaskType.SYNTHESIZE, TaskType.META_REVIEW, TaskType.EVOLVE}),
}


def _due_companions(stats: SchedulerStats, budget: Budget) -> tuple[SupervisorDecision, ...]:
    """Feedback must precede its overview; ranking/evolution chains return to
    the loop point and cannot hand control back to a companion primary."""
    checks = (
        _check_meta_review_cadence(stats),
        _check_research_overview_cadence(stats, budget),
    )
    return tuple(check for check in checks if check is not None)


def stack_companions(
    decision: SupervisorDecision, stats: SchedulerStats, budget: Budget
) -> SupervisorDecision:
    """Primary identity drives counters and settlement. Serialize companions:
    parallel successors fork the checkpoint chain."""
    if decision.terminate or decision.next_task in _UNSTACKABLE_TASKS:
        return decision
    actions = tuple(
        {
            "action": ENQUEUE_ACTION,
            "task_type": companion.next_task.value,
            "reason": companion.reason,
        }
        for companion in _due_companions(stats, budget)
        if decision.next_task not in _COMPANION_CONFLICTS[companion.next_task]
    )
    if not actions:
        return decision
    return dataclasses.replace(decision, queue_actions=(*decision.queue_actions, *actions))
