"""Persisted node keys require a migration before renaming."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science.scheduling.funnel import is_terminal_depth_pass, terminal_depth_owed
from co_scientist.science.scheduling.models import TaskType, stacked_task_values

logger = logging.getLogger(__name__)


# EVOLVE needs critique first; keep task keys aligned with ALLOWED_LOOP_TASKS.
TASK_ROUTES: dict[str, str] = {
    "generate": "generate",
    "reflect": "review",
    "rank": "safety_screen",
    "evolve": "meta_review",
    "meta_review": "meta_review",
    "proximity": "proximity",
    # Periodic synthesis returns to the loop; terminal synthesis ends the run.
    "synthesize": "research_overview",
    "terminate": "research_overview",
}


def _companion_successor(state: WorkflowState, after: str) -> str | None:
    """None delegates non-companions to their ordinary route."""
    # Companions precede the primary; next_task keeps its primary identity.
    companions = stacked_task_values(state.get("supervisor_queue_actions") or [])
    if after not in companions:
        return None
    remaining = companions[companions.index(after) + 1 :]
    value = remaining[0] if remaining else (state.get("next_task") or "")
    return TASK_ROUTES.get(value, "research_overview")


def route_next_task(state: WorkflowState) -> str:
    """A missing scheduler decision falls back to terminal synthesis.
    Finalists without depth get it before the terminal overview."""
    next_task = state.get("next_task") or "terminate"
    companions = stacked_task_values(state.get("supervisor_queue_actions") or [])
    node = TASK_ROUTES.get(companions[0] if companions else next_task, "research_overview")
    if not companions and next_task == TaskType.TERMINATE.value and terminal_depth_owed(state):
        node = "comprehensive_reflection"
    logger.info("Orchestrator routing next_task=%s -> %s", next_task, node)
    return node


def route_after_deep_verification(state: WorkflowState) -> str:
    """The terminal depth pass reports; every other pass ranks."""
    return "research_overview" if is_terminal_depth_pass(state) else "ranking"


# Standalone critique returns to the loop; EVOLVE's critique precedes evolution.
_AFTER_META_REVIEW: dict[str, str] = {
    "evolve": "evolve",
    "meta_review": "orchestrator",
}


def route_after_meta_review(state: WorkflowState) -> str:
    """EVOLVE requires critique first; periodic critique returns to the loop."""
    stacked = _companion_successor(state, TaskType.META_REVIEW.value)
    if stacked is not None:
        return stacked
    next_task = state.get("next_task") or "terminate"
    fixed = _AFTER_META_REVIEW.get(next_task)
    if fixed is not None:
        return fixed
    return TASK_ROUTES.get(next_task, "research_overview")


def route_after_research_overview(state: WorkflowState) -> str | None:
    """Only terminal synthesis finalizes; companions must continue."""
    stacked = _companion_successor(state, TaskType.SYNTHESIZE.value)
    if stacked is not None:
        return stacked
    if state.get("next_task") == TaskType.SYNTHESIZE.value:
        return "orchestrator"
    return None


@dataclass(frozen=True)
class LiteratureGated:
    """Committed MCP availability selects the retrieval flow."""

    on: str
    off: str

    def pick(self, enabled: bool) -> str:
        return self.on if enabled else self.off


Resolver = Callable[[WorkflowState], str | None]
Route = str | LiteratureGated | Resolver


# State-dependent successors resolve only after the producing node commits.
WORKFLOW_ROUTES: dict[str, Route] = {
    "supervisor": LiteratureGated(on="literature_review", off="generate"),
    "literature_review": "generate",
    "generate": LiteratureGated(on="reflection", off="review"),
    "reflection": "review",
    "review": "comprehensive_reflection",
    "comprehensive_reflection": "safety_screen",
    "safety_screen": "deep_verification",
    "deep_verification": route_after_deep_verification,
    "ranking": "orchestrator",
    "proximity": "orchestrator",
    # Meta-review's successor depends on the decision that scheduled it.
    "meta_review": route_after_meta_review,
    "evolve": "review",
    "orchestrator": route_next_task,
    # Periodic overview continues; terminal overview finalizes.
    "research_overview": route_after_research_overview,
}
