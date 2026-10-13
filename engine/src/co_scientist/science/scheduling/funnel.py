"""Depth is spent on finalists: the ranked leaders the report features.

Every idea gets the safety screen and one screening review and enters the
tournament. Observation, full and simulation reviews, deep verification and
claim checks run only for the top finalists by Elo among ideas that have
played, so a first cycle spends nothing on depth and ideas that lose never
pay for it. A deep-verification verdict, written on success and on failure,
marks an idea as having had its depth.
"""

from __future__ import annotations

from collections.abc import Sequence

from co_scientist.domains.research_state.models import Hypothesis, has_peer_review
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science.scheduling.models import TaskType, TerminationReason
from co_scientist.science.scheduling.tournament import finalist_count, ranked_leaders

# A terminal pass costs about this many provider calls per finalist; it must
# never turn a finished run into a budget failure.
TERMINAL_DEPTH_CALLS_PER_FINALIST = 12

# Budget, clock, task-count and safety stops must stop now, not buy more work.
_DEPTH_FUNDED_STOPS = frozenset(
    {
        TerminationReason.COMPLETED.value,
        TerminationReason.CONVERGED.value,
        TerminationReason.MAX_IDEAS.value,
        TerminationReason.MAX_MATCHES_PER_IDEA.value,
    }
)


def finalists(
    state: WorkflowState, hypotheses: Sequence[Hypothesis] | None = None
) -> list[Hypothesis]:
    pool = hypotheses if hypotheses is not None else (state.get("hypotheses") or [])
    contenders = [h for h in pool if h.is_rankable() and has_peer_review(h)]
    # A pool too small to rank never gets leaders; its ideas are the finalists.
    if len(contenders) < 2:
        return contenders
    return ranked_leaders(contenders, finalist_count(state))


def finalist_ids(
    state: WorkflowState, hypotheses: Sequence[Hypothesis] | None = None
) -> frozenset[str]:
    return frozenset(h.id for h in finalists(state, hypotheses))


def has_depth(hypothesis: Hypothesis) -> bool:
    return hypothesis.deep_verification_verdict is not None


def is_terminal_depth_pass(state: WorkflowState) -> bool:
    return state.get("next_task") == TaskType.TERMINATE.value


def _remaining_calls(state: WorkflowState) -> int | None:
    ceiling = (state.get("budget") or {}).get("max_llm_calls")
    if ceiling is None:
        return None
    spent = int(getattr(state.get("metrics"), "llm_calls", 0) or 0)
    return int(ceiling) - spent


def terminal_depth_owed(state: WorkflowState) -> list[Hypothesis]:
    """Finalists still without depth when the run ends, if the stop can fund
    them; the report then never features an idea it did not examine."""
    if state.get("termination_reason") not in _DEPTH_FUNDED_STOPS:
        return []
    owed = [h for h in finalists(state) if not has_depth(h)]
    remaining = _remaining_calls(state)
    if remaining is not None and remaining < len(owed) * TERMINAL_DEPTH_CALLS_PER_FINALIST:
        return []
    return owed
