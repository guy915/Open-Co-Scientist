from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from co_scientist.core.constants import MEDIUM_TEMPERATURE
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.core.prompt_layout import render_cacheable_prompt
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
)
from co_scientist.science.scheduling import (
    Budget,
    SchedulerStats,
    SupervisorDecision,
    TaskType,
    TerminationReason,
    decide_next_task,
    required_transition,
    validate_decision,
)

logger = logging.getLogger(__name__)


# Unsafe content stops immediately; cancellation is enforced by durable dispatch
# cessation rather than a scheduler decision.
_IMMEDIATE_STOP_REASONS = frozenset({TerminationReason.SAFETY})


def _owed_coverage_is_affordable(stats: SchedulerStats) -> bool:
    """Read actual settlement allowance, not a baseline task the model may
    divert; deferral must see the same debt as the forced scheduler check."""
    allowance = stats.settlement_allowance
    return stats.owed_coverage_rounds > 0 and (allowance is None or allowance > 0)


def _owed_review_is_affordable(
    stats: SchedulerStats, termination_reason: TerminationReason
) -> bool:
    """Spent issue markers bound review deferral; provider-budget exhaustion
    cannot defer because the forced call would fail at its admission seam."""
    if termination_reason is TerminationReason.BUDGET:
        return False
    return stats.owed_review_count > 0


def _budget_exceeded(limit: float | None, value: float) -> bool:
    return limit is not None and value >= limit


def _max_ideas_exceeded(stats: SchedulerStats, budget: Budget) -> bool:
    """Review backlog must drain before a pool ceiling can stop newly
    admitted ideas."""
    limit = budget.max_ideas
    return limit is not None and stats.unreviewed_count == 0 and stats.pool_size >= limit


def _max_matches_per_idea_exceeded(stats: SchedulerStats, budget: Budget) -> bool:
    limit = budget.max_matches_per_idea
    return limit is not None and stats.rankable_count >= 2 and stats.match_coverage >= limit


def _hard_stop_checks(
    stats: SchedulerStats, budget: Budget
) -> tuple[tuple[bool, TerminationReason, str], ...]:
    return (
        (
            stats.safety_blocked,
            TerminationReason.SAFETY,
            "safety block halted the run",
        ),
        (
            _budget_exceeded(budget.max_llm_calls, stats.llm_calls),
            TerminationReason.BUDGET,
            "LLM-call budget exhausted",
        ),
        (
            _budget_exceeded(budget.max_tasks, stats.tasks_run),
            TerminationReason.MAX_TASKS,
            "task budget exhausted",
        ),
        (
            _budget_exceeded(budget.max_wall_clock_s, stats.elapsed_s),
            TerminationReason.WALL_CLOCK,
            "wall-clock budget exhausted",
        ),
        (
            _max_ideas_exceeded(stats, budget),
            TerminationReason.MAX_IDEAS,
            "idea-pool budget exhausted",
        ),
        (
            _max_matches_per_idea_exceeded(stats, budget),
            TerminationReason.MAX_MATCHES_PER_IDEA,
            "match budget exhausted",
        ),
    )


def _hard_stop_reason(
    stats: SchedulerStats, budget: Budget
) -> tuple[TerminationReason, str] | None:
    for triggered, reason, message in _hard_stop_checks(stats, budget):
        if triggered:
            return reason, message
    return None


def _hard_stop(
    stats: SchedulerStats,
    budget: Budget,
    baseline: SupervisorDecision,
) -> SupervisorDecision | None:
    reason = _hard_stop_reason(stats, budget)
    if reason is None:
        return baseline if baseline.terminate else None
    termination_reason, message = reason
    if (
        _owed_coverage_is_affordable(stats) or _owed_review_is_affordable(stats, termination_reason)
    ) and termination_reason not in _IMMEDIATE_STOP_REASONS:
        # Finite allowance/issue markers bound cleanup deferral; no failed
        # review may refill its spent override.
        return None
    return SupervisorDecision(
        next_task=TaskType.TERMINATE,
        reason=message,
        terminate=True,
        termination_reason=termination_reason,
    )


_PRODUCTIVE_TASKS = (
    TaskType.GENERATE,
    TaskType.REFLECT,
    TaskType.RANK,
    TaskType.EVOLVE,
    TaskType.PROXIMITY,
)

WORK_TASKS = frozenset({TaskType.GENERATE, TaskType.EVOLVE})

# Lax providers do not enforce these bounds; in-process validation must. Top-
# level required fields may be backfilled, unlike nested queue items.
_DECISION_SCHEMA: dict[str, Any] = {
    "name": "supervisor_allocation",
    "schema": {
        "type": "object",
        "properties": {
            "next_task": {
                "type": "string",
                "enum": [task.value for task in _PRODUCTIVE_TASKS],
            },
            "reason": {"type": "string", "minLength": 1},
            "priority": {"type": "integer", "minimum": 0, "maximum": 100},
            "queue_actions": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["reprioritize", "cancel", "retry"],
                        },
                        "task_id": {"type": "string", "minLength": 1},
                        "priority": {
                            "type": ["integer", "null"],
                            "minimum": 0,
                            "maximum": 100,
                        },
                        "reason": {"type": "string", "minLength": 1},
                    },
                    "required": ["action", "task_id", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["next_task", "reason"],
        "additionalProperties": False,
    },
}


def _planning_prompt(state: WorkflowState, stats: SchedulerStats, budget: Budget) -> str:
    context = {
        "research_plan": state.get("supervisor_guidance") or {},
        "statistics": stats.to_dict(),
        "budget": budget.to_dict(),
        "recent_tasks": state.get("task_history", [])[-12:],
        "meta_review": state.get("meta_review") or {},
        "pending_steering": state.get("pending_steering") or False,
        "held_for_review": len(state.get("held_for_review", [])),
        "durable_task_queue": state.get("durable_task_queue") or [],
    }
    instructions = (
        "You are the adaptive Supervisor for a scientific co-research system. "
        "Allocate the single most valuable next specialist task from generate, "
        "reflect, rank, evolve, or proximity. Use the live state rather than a "
        "fixed phase order. Prioritize unresolved verification and review work "
        "as hypotheses mature, incorporate scientist steering immediately, and "
        "balance exploration against improvement. Explain the observable basis "
        "for the allocation without revealing hidden chain-of-thought. Use "
        "queue_actions only when a listed queued/failed task should be "
        "reprioritized, cancelled as superseded, or retried because state has "
        "materially changed. Never target an unlisted task."
    )
    return render_cacheable_prompt(
        instructions,
        f"Research goal: {state['research_goal']}",
        "Live shared memory:\n" + json.dumps(context, sort_keys=True, default=str),
        "Allocate the next task or stop using the live state.",
    )


def _repeats_without_iteration_progress(
    state: WorkflowState,
    stats: SchedulerStats,
    proposed: SupervisorDecision,
    baseline: SupervisorDecision,
) -> bool:
    if proposed.next_task is baseline.next_task:
        return False
    return any(
        str(record.get("task_type")) == proposed.next_task.value
        and int(record.get("iteration", -1)) == stats.iteration
        for record in state.get("task_history", [])
    )


def _needs_queue_adjudication(state: WorkflowState) -> bool:
    """Spent failed tasks have no automatic revival; only model queue actions
    can recover them, so forced transitions still need this planning call."""
    return any(
        str(entry.get("status")) == "failed" for entry in state.get("durable_task_queue") or ()
    )


async def choose_supervisor_task(
    state: WorkflowState,
    stats: SchedulerStats,
    budget: Budget,
) -> tuple[SupervisorDecision, str, int]:
    """Required transitions need no uncached planning; consult the model only
    for open allocation or failed-queue adjudication."""
    forced = required_transition(stats, budget)
    baseline = forced if forced is not None else decide_next_task(stats, budget)

    stop = _hard_stop(stats, budget, baseline)
    if stop is not None:
        return stop, "hard-invariant", 0

    if forced is not None and not _needs_queue_adjudication(state):
        return validate_decision(forced, stats), "required-transition", 0

    try:
        validated = await _call_supervisor_planner(state, stats, budget)
        decision, provenance = _resolve_planner_decision(state, stats, budget, baseline, validated)
        return decision, provenance, 1
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        logger.warning("Supervisor allocation failed; using fallback: %s", exc)
        fallback = validate_decision(baseline, stats)
        return fallback, "reconstructed-fallback", 1


async def _call_supervisor_planner(
    state: WorkflowState, stats: SchedulerStats, budget: Budget
) -> SupervisorDecision:
    """The cohort waits here; allocation latency cannot overlap other work."""
    response = await call_llm_json(
        prompt=_planning_prompt(state, stats, budget),
        spec=CompletionSpec(
            role="orchestrator",
            model_name=state["model_name"],
            temperature=MEDIUM_TEMPERATURE,
            json_schema=_DECISION_SCHEMA,
        ),
        options=LLMCallOptions(
            run_id=state.get("run_id"),
            prompt_name="supervisor_allocation",
        ),
    )
    # Optional priority defaults to 50; integral JSON floats normalize to int
    # after schema bounds, while parsed queue actions retain their typed values.
    proposed = SupervisorDecision(
        next_task=TaskType(str(response["next_task"])),
        reason=str(response["reason"]),
        priority=max(0, min(100, int(response.get("priority", 50)))),
        queue_actions=tuple(response.get("queue_actions") or ()),
    )
    return validate_decision(proposed, stats)


def _overruled(baseline: SupervisorDecision, validated: SupervisorDecision) -> SupervisorDecision:
    """Overruled next tasks keep independent recovery queue actions;
    termination short-circuits before any carry-through can revive work."""
    if not validated.queue_actions:
        return baseline
    return dataclasses.replace(baseline, queue_actions=validated.queue_actions)


def _resolve_planner_decision(
    state: WorkflowState,
    stats: SchedulerStats,
    budget: Budget,
    baseline: SupervisorDecision,
    validated: SupervisorDecision,
) -> tuple[SupervisorDecision, str]:
    if _repeats_without_iteration_progress(state, stats, validated, baseline):
        # Repeated maintenance without work progress is a loop; fall back to
        # deterministic policy while preserving recovery queue actions.
        return _overruled(baseline, validated), "hard-invariant"
    if (
        not stats.pending_steering
        and stats.iteration >= budget.max_iterations
        and validated.next_task in WORK_TASKS
    ):
        # After exploration budget, required cleanup remains allowed but model
        # allocation cannot grow the pool again.
        return _overruled(baseline, validated), "hard-invariant"
    return validated, "model"
