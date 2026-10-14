from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from co_scientist.core.constants import (
    INITIAL_ELO_RATING,
    PROGRESS_ORCHESTRATOR_DECISION,
)
from co_scientist.domains.research_state.models import (
    Hypothesis,
    MetricDeltas,
    create_metrics_update,
    has_peer_review,
    phase_message,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import current_run_call_count
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.scheduling import (
    Budget,
    SchedulerStats,
    SupervisorDecision,
    TaskRecord,
    TaskStatus,
    TaskType,
    policy,
    stacked_task_values,
)
from co_scientist.science.scheduling.owed_review import (
    mark_owed_review_issued,
    owed_review_targets,
)
from co_scientist.science.scheduling.owed_review import (
    owed_review_count as _owed_review_count,
)
from co_scientist.science.scheduling.tournament import coverage_floor, remaining_ranking_rounds
from co_scientist.science.supervisor.supervisor_decision import (
    WORK_TASKS,
    choose_supervisor_task,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _StatsScalars:
    pool_size: int
    reviewed: int
    owed_review: int
    rankable_count: int
    total_matches: int
    avg_coverage: float
    unmatched_rankable_count: int
    owed_coverage_rounds: int
    top_elo: int
    llm_calls: int
    gen_yield: float
    evo_yield: float
    elapsed_s: float


def _default_budget(state: WorkflowState) -> Budget:
    raw = state.get("budget")
    if raw:
        return Budget.from_dict(raw)
    return Budget(max_iterations=state.get("max_iterations", 0))


def _yields(pool_size: int, book: dict[str, Any]) -> tuple[float, float]:
    delta = max(0, pool_size - int(book.get("pool_at_last_decision", pool_size)))
    last = book.get("last_work_task")
    if last == TaskType.GENERATE.value:
        return float(delta), 0.0
    if last == TaskType.EVOLVE.value:
        return 0.0, float(delta)
    return 0.0, 0.0


def _rank_stable_cycles(top_elo: int, total_matches: int, book: dict[str, Any]) -> int:
    """Untouched seed Elo and the first decision cannot establish
    convergence."""
    prev = book.get("prev_top_elo")
    prior = int(book.get("rank_stable_cycles", 0))
    if total_matches > 0 and prev is not None and top_elo == int(prev):
        return prior + 1
    return 0


def _compute_stats(state: WorkflowState, book: dict[str, Any]) -> SchedulerStats:
    hyps: list[Hypothesis] = state["hypotheses"]
    pool_size = len(hyps)
    rankable_count, avg_coverage, unmatched = _rankable_coverage(hyps)
    llm_calls, gen_yield, evo_yield, elapsed_s = _scheduler_scalars(state, book, pool_size)
    scalars = _StatsScalars(
        pool_size=pool_size,
        reviewed=sum(1 for h in hyps if has_peer_review(h)),
        owed_review=_owed_review_count(hyps),
        rankable_count=rankable_count,
        total_matches=sum(h.total_matches for h in hyps),
        avg_coverage=avg_coverage,
        unmatched_rankable_count=unmatched,
        # Coverage debt and finite settlement allowance must use the same floor.
        owed_coverage_rounds=coverage_floor(hyps),
        top_elo=max((h.elo_rating for h in hyps), default=INITIAL_ELO_RATING),
        llm_calls=llm_calls,
        gen_yield=gen_yield,
        evo_yield=evo_yield,
        elapsed_s=elapsed_s,
    )
    return _build_scheduler_stats(state, book, scalars)


def _rankable_coverage(
    hyps: list[Hypothesis],
) -> tuple[int, float, int]:
    """Excluded ideas cannot earn coverage; count rankable ideas only and
    track unmatched individuals separately from an average."""
    rankable = [h for h in hyps if h.is_rankable()]
    rankable_count = len(rankable)
    rankable_matches = sum(h.total_matches for h in rankable)
    avg_coverage = rankable_matches / rankable_count if rankable_count else 0.0
    # Count unmatched over the same reviewed, rankable population as the quoted
    # floor so the observable reason cannot contradict its debt.
    unmatched = sum(1 for h in rankable if h.total_matches == 0 and has_peer_review(h))
    return rankable_count, avg_coverage, unmatched


def _scheduler_scalars(
    state: WorkflowState, book: dict[str, Any], pool_size: int
) -> tuple[int, float, float, float]:
    """Provider-seam counts include unreported calls; max with checkpoint
    metrics prevents restart resetting a resumed run to a fresh budget."""
    metrics = state.get("metrics")
    reported = metrics.llm_calls if metrics is not None else 0
    run_id = state.get("run_id")
    seam_count = current_run_call_count(run_id) if run_id else 0
    llm_calls = max(seam_count, reported)
    gen_yield, evo_yield = _yields(pool_size, book)
    start = state.get("start_time") or time.time()
    return llm_calls, gen_yield, evo_yield, time.time() - start


def _meta_review_gap(
    book: dict[str, Any], scalars: _StatsScalars, iteration: int
) -> tuple[int, int]:
    """Dedup removes ideas and their match tallies, so material deltas may
    shrink and must floor at zero."""
    cycles = iteration - int(book.get("iteration_at_last_meta_review", 0))
    material = (scalars.reviewed + scalars.total_matches) - int(
        book.get("feedback_at_last_meta_review", 0)
    )
    return max(0, cycles), max(0, material)


def _cadence_signals(
    state: WorkflowState,
    book: dict[str, Any],
    scalars: _StatsScalars,
    pool_size: int,
    iteration: int,
) -> dict[str, Any]:
    meta_cycles, meta_material = _meta_review_gap(book, scalars, iteration)
    return {
        "pool_grew_since_proximity": (
            pool_size > int(book.get("pool_at_last_proximity", pool_size))
        ),
        "rank_stable_cycles": _rank_stable_cycles(scalars.top_elo, scalars.total_matches, book),
        "iterations_since_meta_review": meta_cycles,
        "feedback_since_meta_review": meta_material,
        "iterations_since_research_overview": max(
            0,
            iteration - int(book.get("iteration_at_last_research_overview", 0)),
        ),
        "evolved_since_stable": bool(book.get("evolved_since_stable", False)),
    }


def _build_scheduler_stats(
    state: WorkflowState,
    book: dict[str, Any],
    scalars: _StatsScalars,
) -> SchedulerStats:
    pool_size = scalars.pool_size
    iteration = state.get("current_iteration", 0)
    cadence = _cadence_signals(state, book, scalars, pool_size, iteration)
    return SchedulerStats(
        pool_size=pool_size,
        reviewed_count=scalars.reviewed,
        unreviewed_count=pool_size - scalars.reviewed,
        owed_review_count=scalars.owed_review,
        rankable_count=scalars.rankable_count,
        total_matches=scalars.total_matches,
        match_coverage=scalars.avg_coverage,
        unmatched_rankable_count=scalars.unmatched_rankable_count,
        owed_coverage_rounds=scalars.owed_coverage_rounds,
        settlement_allowance=book.get("settlement_allowance"),
        owed_at_last_settlement=book.get("owed_at_last_settlement"),
        tournament_rounds_remaining=remaining_ranking_rounds(state, state.get("hypotheses") or []),
        top_elo=scalars.top_elo,
        generation_yield=scalars.gen_yield,
        evolution_yield=scalars.evo_yield,
        iteration=iteration,
        last_work_task=_task_type_or_none(book.get("last_work_task")),
        llm_calls=scalars.llm_calls,
        tasks_run=len(state.get("task_history", [])),
        elapsed_s=scalars.elapsed_s,
        pending_steering=bool(state.get("pending_steering")),
        cancelled=bool(state.get("cancel_requested")),
        safety_blocked=bool(state.get("safety_blocked")),
        # Old/restored state missing the field keeps meta-review enabled.
        meta_review_enabled=state.get("enable_meta_review", True) is not False,
        **cadence,
    )


def _task_type_or_none(value: Any) -> TaskType | None:
    if value is None:
        return None
    return TaskType(value)


def _init_bookkeeping(hypotheses: list[Hypothesis]) -> dict[str, Any]:
    """Post-generation anchors create a yield tie and no proximity backlog,
    retaining first-cycle leader evolution before later work alternates."""
    pool_size = len(hypotheses)
    return {
        "prev_top_elo": None,
        "rank_stable_cycles": 0,
        "pool_at_last_proximity": pool_size,
        "pool_at_last_decision": pool_size,
        "last_work_task": TaskType.GENERATE.value,
        "settlement_allowance": None,
        "owed_at_last_settlement": None,
        # Zero feedback anchors let the first firing use initial-generation
        # reviews rather than treating them as already consumed.
        "iteration_at_last_meta_review": 0,
        "feedback_at_last_meta_review": 0,
        "iteration_at_last_research_overview": 0,
        "evolved_since_stable": False,
    }


_META_REVIEW_ROUTED_TASKS = frozenset({TaskType.META_REVIEW, TaskType.EVOLVE})

_ITERATION_ADVANCING_TASKS = frozenset({TaskType.GENERATE, TaskType.EVOLVE})


def _routes_through_meta_review(decision: SupervisorDecision) -> bool:
    """Primary, evolve-route and stacked firings consume cadence; without re-
    anchoring, companions repeat at every loop point."""
    if decision.next_task in _META_REVIEW_ROUTED_TASKS:
        return True
    return TaskType.META_REVIEW.value in stacked_task_values(decision.queue_actions)


def _schedules_research_overview(decision: SupervisorDecision) -> bool:
    """Stacked and primary overview firings both consume cadence; re-anchor
    either to prevent repeated companion work."""
    if decision.next_task is TaskType.SYNTHESIZE:
        return True
    return TaskType.SYNTHESIZE.value in stacked_task_values(decision.queue_actions)


def _research_overview_anchor(
    book: dict[str, Any],
    stats: SchedulerStats,
    decision: SupervisorDecision,
) -> int:
    """Overview is maintenance, not a work cycle; re-anchor at scheduling
    because no iteration advance will otherwise clear its due threshold."""
    if not _schedules_research_overview(decision):
        return int(book.get("iteration_at_last_research_overview", 0))
    return stats.iteration


def _feedback_total(stats: SchedulerStats) -> int:
    return stats.reviewed_count + stats.total_matches


def _meta_review_anchors(
    book: dict[str, Any],
    stats: SchedulerStats,
    decision: SupervisorDecision,
) -> tuple[int, int]:
    """EVOLVE advances when scheduled; use the resulting iteration so the
    next decision cannot fund a second unearned feedback firing."""
    if not _routes_through_meta_review(decision):
        return (
            int(book.get("iteration_at_last_meta_review", 0)),
            int(book.get("feedback_at_last_meta_review", 0)),
        )
    advanced = 1 if decision.next_task in _ITERATION_ADVANCING_TASKS else 0
    return stats.iteration + advanced, _feedback_total(stats)


def _evolved_since_stable(
    book: dict[str, Any], stats: SchedulerStats, decision: SupervisorDecision
) -> bool:
    """Each new stagnation episode earns evolution; a pre-stability attempt
    cannot count as responding to current stagnation."""
    if stats.rank_stable_cycles < 1:
        return False
    if decision.next_task is TaskType.EVOLVE:
        return True
    return bool(book.get("evolved_since_stable", False))


def _is_settlement_rank(stats: SchedulerStats, decision: SupervisorDecision) -> bool:
    """Charge only rounds requested by the policy coverage check; ordinary
    calibration must not drain settlement allowance before its episode."""
    return decision.next_task is TaskType.RANK and policy._check_owed_coverage(stats) is not None


def _is_owed_review_override(stats: SchedulerStats, budget: Budget) -> bool:
    """Spend at the forced check even if queue adjudication diverts its task;
    executed-task marking would endlessly rearm the override."""
    return policy._check_owed_review(stats, budget) is not None


def _owed_review_override_marks(
    stats: SchedulerStats, budget: Budget, hypotheses: list[Hypothesis]
) -> list[Hypothesis]:
    """Issue spends the attempt regardless of success. Return the full pool
    when marking: a bare subset would replace and drop all other ideas."""
    if not _is_owed_review_override(stats, budget):
        return []
    for hypothesis in owed_review_targets(hypotheses):
        mark_owed_review_issued(hypothesis)
    return hypotheses


def _settled_allowance(
    book: dict[str, Any],
    stats: SchedulerStats,
    decision: SupervisorDecision,
    hypotheses: list[Hypothesis],
) -> tuple[int | None, int | None]:
    """One owed-round measure opens, sizes and closes the finite episode;
    never refill until debt reaches zero or cleanup could loop forever."""
    if _is_settlement_rank(stats, decision):
        allowance = book.get("settlement_allowance")
        if allowance is None:
            # Use the tournament floor; zero-match counts miss ideas one match
            # short and would underfund the settlement allowance.
            allowance = coverage_floor(hypotheses)
        return (
            max(0, int(allowance) - 1),
            stats.owed_coverage_rounds,
        )
    if stats.owed_coverage_rounds == 0:
        return None, None
    return (
        book.get("settlement_allowance"),
        book.get("owed_at_last_settlement"),
    )


def _next_bookkeeping(
    book: dict[str, Any],
    stats: SchedulerStats,
    decision: SupervisorDecision,
    hypotheses: list[Hypothesis],
) -> dict[str, Any]:
    updated = dict(book)
    updated["prev_top_elo"] = stats.top_elo
    updated["rank_stable_cycles"] = stats.rank_stable_cycles
    updated["pool_at_last_decision"] = stats.pool_size
    if decision.next_task is TaskType.PROXIMITY:
        updated["pool_at_last_proximity"] = stats.pool_size
    if decision.next_task in (TaskType.GENERATE, TaskType.EVOLVE):
        updated["last_work_task"] = decision.next_task.value
    allowance, last_owed = _settled_allowance(book, stats, decision, hypotheses)
    updated["settlement_allowance"] = allowance
    updated["owed_at_last_settlement"] = last_owed
    meta_iteration, meta_feedback = _meta_review_anchors(book, stats, decision)
    updated["iteration_at_last_meta_review"] = meta_iteration
    updated["feedback_at_last_meta_review"] = meta_feedback
    updated["iteration_at_last_research_overview"] = _research_overview_anchor(
        book, stats, decision
    )
    updated["evolved_since_stable"] = _evolved_since_stable(book, stats, decision)
    return updated


@dataclasses.dataclass(frozen=True)
class _DecisionOutcome:
    decision: SupervisorDecision
    decision_provenance: str
    iteration: int
    observable_reason: str
    termination_reason_value: str | None
    llm_calls: int = 0


def _appended_task_record(
    state: WorkflowState,
    decision: SupervisorDecision,
    iteration: int,
    observable_reason: str,
) -> list[dict[str, Any]]:
    record = TaskRecord(
        task_type=decision.next_task,
        status=(TaskStatus.COMPLETED if decision.terminate else TaskStatus.QUEUED),
        reason=observable_reason,
        iteration=iteration,
        termination_reason=decision.termination_reason,
    )
    history = list(state.get("task_history", []))
    serialized = record.to_dict()
    serialized["priority"] = decision.priority
    # Preserve model rationale for audit, but do not present it as observed
    # activity.
    serialized["planner_reason"] = decision.reason
    history.append(serialized)
    return history


def _observable_decision_reason(
    stats: SchedulerStats,
    decision: SupervisorDecision,
) -> str:
    match_count = stats.total_matches // 2
    reason = (
        f"Supervisor selected {decision.next_task.value} from live state: "
        f"{stats.pool_size} hypotheses, {stats.reviewed_count} reviewed, "
        f"{match_count} committed matches, iteration {stats.iteration}."
    )
    if stats.pending_steering:
        reason += " Scientist feedback is pending incorporation."
    return reason


async def orchestrator_node(state: WorkflowState) -> dict[str, Any]:
    book = state.get("orchestrator_state") or _init_bookkeeping(state["hypotheses"])
    (
        stats,
        decision,
        decision_provenance,
        llm_calls,
    ) = await _run_supervisor_decision(state, book)
    iteration, observable_reason, termination_reason_value = _decision_context(
        state, stats, decision
    )
    outcome = _DecisionOutcome(
        decision=decision,
        decision_provenance=decision_provenance,
        iteration=iteration,
        observable_reason=observable_reason,
        termination_reason_value=termination_reason_value,
        llm_calls=llm_calls,
    )
    await _emit_orchestrator_decision(state, outcome)
    return _orchestrator_result(state, book, stats, outcome)


async def _run_supervisor_decision(
    state: WorkflowState, book: dict[str, Any]
) -> tuple[SchedulerStats, SupervisorDecision, str, int]:
    stats = _compute_stats(state, book)
    budget = _default_budget(state)
    decision, decision_provenance, llm_calls = await choose_supervisor_task(state, stats, budget)
    # Stack companions after planner guards; rebuilding the baseline earlier
    # would discard already attached companions.
    return (
        stats,
        policy.stack_companions(decision, stats, budget),
        decision_provenance,
        llm_calls,
    )


def _advance_iteration(state: WorkflowState, decision: SupervisorDecision) -> int:
    iteration = state.get("current_iteration", 0)
    if decision.next_task in WORK_TASKS:
        iteration += 1
    return iteration


def _decision_context(
    state: WorkflowState, stats: SchedulerStats, decision: SupervisorDecision
) -> tuple[int, str, str | None]:
    iteration = _advance_iteration(state, decision)
    observable_reason = _observable_decision_reason(stats, decision)
    termination_reason_value = (
        decision.termination_reason.value if decision.termination_reason is not None else None
    )
    return iteration, observable_reason, termination_reason_value


async def _emit_orchestrator_decision(
    state: WorkflowState,
    outcome: _DecisionOutcome,
) -> None:
    decision = outcome.decision
    logger.info(
        "Orchestrator scheduled %s (iteration %s): %s",
        decision.next_task.value,
        outcome.iteration,
        outcome.observable_reason,
    )
    await emit_progress(
        state,
        "orchestrator_decision",
        outcome.observable_reason,
        PROGRESS_ORCHESTRATOR_DECISION,
        next_task=decision.next_task.value,
        termination_reason=outcome.termination_reason_value,
        decision_provenance=outcome.decision_provenance,
    )


def _orchestrator_result(
    state: WorkflowState,
    book: dict[str, Any],
    stats: SchedulerStats,
    outcome: _DecisionOutcome,
) -> dict[str, Any]:
    decision = outcome.decision
    return {
        "next_task": decision.next_task.value,
        "next_task_priority": decision.priority,
        "supervisor_queue_actions": list(decision.queue_actions),
        "task_history": _appended_task_record(
            state, decision, outcome.iteration, outcome.observable_reason
        ),
        "orchestrator_state": _next_bookkeeping(book, stats, decision, state["hypotheses"]),
        "hypotheses": _owed_review_override_marks(
            stats, _default_budget(state), state["hypotheses"]
        ),
        "supervisor_decision_provenance": outcome.decision_provenance,
        "current_iteration": outcome.iteration,
        # Consume steering once work to incorporate it is scheduled; do not
        # trigger repeated cycles for the same input.
        "pending_steering": False,
        "termination_reason": outcome.termination_reason_value,
        "messages": phase_message(
            "orchestrator",
            outcome.observable_reason,
            next_task=decision.next_task.value,
        ),
        "metrics": create_metrics_update(deltas=MetricDeltas(llm_calls=outcome.llm_calls)),
    }
