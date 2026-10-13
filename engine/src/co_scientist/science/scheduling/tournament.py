import logging
from collections.abc import Sequence

from co_scientist.core.constants import (
    TOURNAMENT_MATCHES_PER_HYPOTHESIS,
    TOURNAMENT_MIN_MATCHES_PER_HYPOTHESIS,
)
from co_scientist.domains.research_state.models import Hypothesis, has_peer_review, rank_by_elo
from co_scientist.domains.research_state.state import WorkflowState

logger = logging.getLogger(__name__)

# Checkpoints before the per-tier count featured five ideas.
DEFAULT_FINALISTS = 5


def finalist_count(state: WorkflowState) -> int:
    raw = (state.get("budget") or {}).get("finalists")
    try:
        return max(1, int(raw)) if raw is not None else DEFAULT_FINALISTS
    except (TypeError, ValueError):
        return DEFAULT_FINALISTS


def ranked_leaders(hypotheses: Sequence[Hypothesis], count: int) -> list[Hypothesis]:
    # Seed Elo is not an earned rank.
    return rank_by_elo([h for h in hypotheses if h.total_matches])[:count]


def consumed_tournament_rounds(state: WorkflowState) -> int:
    """Dedup removes hypotheses and their tallies; only accumulated run
    metrics keep spent match budget from being refunded."""
    metrics = state.get("metrics")
    return max(0, int(getattr(metrics, "tournaments_count", 0) or 0))


def coverage_floor(hypotheses: list[Hypothesis]) -> int:
    """Only reviewed rankable ideas owe matches. Bound by distinct pairs and
    fund the largest lone-idea deficit, not just half the owed slots."""
    rankable = [h for h in hypotheses if h.is_rankable() and has_peer_review(h)]
    if len(rankable) < 2:
        return 0
    owed_per_idea = [
        max(0, TOURNAMENT_MIN_MATCHES_PER_HYPOTHESIS - h.total_matches) for h in rankable
    ]
    owed = sum(owed_per_idea)
    if not owed:
        return 0
    max_pairs = len(rankable) * (len(rankable) - 1) // 2
    rounds = max((owed + 1) // 2, max(owed_per_idea))
    return min(rounds, max_pairs)


def _tournament_budget(state: WorkflowState, hypotheses: list[Hypothesis]) -> int:
    """Evolution adds ideas after the tier allowance runs out; scaling by
    pool size funds later children beyond minimal coverage."""
    configured = max(1, int(state.get("tournament_pairs") or len(hypotheses)))
    rankable = sum(1 for h in hypotheses if h.is_rankable())
    scaled = (rankable * TOURNAMENT_MATCHES_PER_HYPOTHESIS + 1) // 2
    return max(configured, scaled)


def remaining_ranking_rounds(state: WorkflowState, hypotheses: list[Hypothesis]) -> int:
    """This is a whole-run allowance; coverage still funds unmatched reviewed
    ideas so seed Elo is not presented as an earned tournament rating."""
    budget = _tournament_budget(state, hypotheses)
    remaining = max(0, budget - consumed_tournament_rounds(state))
    floor = coverage_floor(hypotheses)
    if floor > remaining:
        logger.info(
            "Tournament budget spent (%s of %s), but %s round(s) still owed "
            "to hypotheses that have never been matched",
            remaining,
            budget,
            floor,
        )
        return floor
    logger.info("Tournament budget: %s of %s rounds remaining", remaining, budget)
    return remaining
