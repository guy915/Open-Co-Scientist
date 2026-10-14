import logging
from typing import Any, NamedTuple

from co_scientist.core.constants import (
    INITIAL_ELO_RATING,
    PROGRESS_TOURNAMENT_COMPLETE,
    PROGRESS_TOURNAMENT_START,
    truncate,
)
from co_scientist.domains.research_state.models import (
    Hypothesis,
    rank_for_publication,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.ranking.ranking_debate import (
    _build_ranking_delta,
)
from co_scientist.science.scheduling.tournament import remaining_ranking_rounds

logger = logging.getLogger(__name__)


class TournamentGuidance(NamedTuple):
    """NamedTuple preserves the historical positional five-field unpacking
    contract."""

    supervisor_guidance: dict[str, Any] | None = None
    tool_registry: Any | None = None
    meta_review: dict[str, Any] | None = None
    run_setup_guidance: str | None = None
    run_focus_guidance: str | None = None


def _gather_tournament_context(state: WorkflowState) -> TournamentGuidance:
    """Snapshot guidance once so every pairing sees identical cross-node
    context."""
    return TournamentGuidance(
        supervisor_guidance=state.get("supervisor_guidance"),
        tool_registry=state.get("tool_registry"),
        meta_review=state.get("meta_review"),
        run_setup_guidance=state.get("run_setup_guidance"),
        run_focus_guidance=state.get("run_focus_guidance"),
    )


def add_to_tournament(hypothesis: Hypothesis) -> bool:
    """Normal models already carry seed Elo; debug logging avoids one no-op
    line per idea crowding the durable run log."""
    if hypothesis.elo_rating:
        logger.debug(
            "hypothesis %s is already in the tournament (elo %s)",
            hypothesis.id,
            hypothesis.elo_rating,
        )
        return False
    hypothesis.elo_rating = INITIAL_ELO_RATING
    return True


def _admit_hypotheses_to_tournament(hypotheses: list[Hypothesis]) -> None:
    for hypothesis in hypotheses:
        add_to_tournament(hypothesis)


async def prepare_ranking_round(
    state: WorkflowState,
    hypotheses: list[Hypothesis],
) -> tuple[int, TournamentGuidance]:
    _admit_hypotheses_to_tournament(hypotheses)
    hypotheses.sort(key=lambda h: (h.score, h.text), reverse=True)
    logger.info(
        "Sorted hypotheses by review score (top score: %.2f)",
        hypotheses[0].score,
    )

    await emit_progress(
        state,
        "tournament_start",
        f"Running tournament with {len(hypotheses)} hypotheses...",
        PROGRESS_TOURNAMENT_START,
    )

    tournament_rounds = remaining_ranking_rounds(state, hypotheses)
    return tournament_rounds, _gather_tournament_context(state)


async def finalize_ranking(
    state: WorkflowState,
    hypotheses: list[Hypothesis],
    matchup_details: list[dict[str, Any]],
    tournament_rounds: int,
    total_llm_calls: int,
) -> dict[str, Any]:
    # Canonical ordering aligns tournament ties and research top-k; publication
    # bands demote undermined ideas despite high Elo.
    hypotheses = sorted(rank_for_publication(hypotheses), key=lambda h: not h.is_rankable())

    logger.info("Tournament complete. Top Elo: %s", hypotheses[0].elo_rating)
    logger.info("Top hypothesis length: %s characters", len(hypotheses[0].text))

    await emit_progress(
        state,
        "tournament_complete",
        f"Tournament complete ({tournament_rounds} rounds)",
        PROGRESS_TOURNAMENT_COMPLETE,
        top_elo=hypotheses[0].elo_rating,
        # Mark UI truncation so an incomplete idea cannot look like a complete
        # short one.
        top_hypothesis=truncate(hypotheses[0].text),
    )

    return _build_ranking_delta(hypotheses, matchup_details, tournament_rounds, total_llm_calls)


_TournamentGuidance = TournamentGuidance
_prepare_ranking_round = prepare_ranking_round
_finalize_ranking_result = finalize_ranking
