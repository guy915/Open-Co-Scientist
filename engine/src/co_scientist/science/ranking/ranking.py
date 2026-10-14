import logging
from dataclasses import replace
from typing import Any, NamedTuple

from co_scientist.core.constants import (
    ELO_K_FACTOR,
)
from co_scientist.domains.research_state.models import BLOCKING_REVIEW_DISPOSITIONS, Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science.ranking.operations import (
    RankingPromptContext,
    apply_ranking_matchup,
    judge_ranking_matchup,
    prepare_ranking_judging_context,
    prepare_ranking_prompt_context,
)
from co_scientist.science.ranking.ranking_debate import (
    calculate_elo_update as calculate_elo_update,
)
from co_scientist.science.ranking.ranking_debate import (
    judge_matchup as judge_matchup,
)
from co_scientist.science.ranking.ranking_debate import (
    match_tier as match_tier,
)
from co_scientist.science.ranking.ranking_lifecycle import (
    _TournamentGuidance as _TournamentGuidance,
)
from co_scientist.science.ranking.ranking_lifecycle import finalize_ranking, prepare_ranking_round
from co_scientist.science.ranking.ranking_matchmaking import (
    build_tournament_pairings,
)
from co_scientist.science.scheduling.tournament import remaining_ranking_rounds

logger = logging.getLogger(__name__)


class _TournamentContext(NamedTuple):
    hypotheses: list[Hypothesis]
    research_goal: str
    current_iteration: int
    prompt: RankingPromptContext


def _select_next_pairing(
    ctx: _TournamentContext,
    index: int,
    judged: set[frozenset[str]],
) -> tuple[Hypothesis, Hypothesis] | None:
    """Select against committed ratings and exclude judged pairs; replaying
    one comparison inflates Elo without adding evidence."""
    candidates = build_tournament_pairings(
        ctx.hypotheses,
        1,
        ctx.research_goal,
        ctx.current_iteration * 10_000 + index,
        judged=judged,
    )
    return candidates[0] if candidates else None


async def _judge_and_commit_matchup(
    state: WorkflowState,
    hyp_a: Hypothesis,
    hyp_b: Hypothesis,
    index: int,
    ctx: _TournamentContext,
) -> tuple[dict[str, Any], int]:
    pair = (hyp_a, hyp_b)
    context = prepare_ranking_judging_context(ctx.prompt, ctx.hypotheses)
    judgement = await judge_ranking_matchup(pair, context, index)
    result = apply_ranking_matchup(
        pair,
        judgement,
        k_factor=int(state.get("elo_k_factor") or ELO_K_FACTOR),
        current_iteration=int(state.get("current_iteration", 0)),
    )
    return result.detail, result.llm_calls


async def _run_one_round(
    state: WorkflowState,
    index: int,
    judged: set[frozenset[str]],
    ctx: _TournamentContext,
) -> tuple[dict[str, Any], int, frozenset[str]] | None:
    pairing = _select_next_pairing(ctx, index, judged)
    if pairing is None:
        return None
    hyp_a, hyp_b = pairing
    detail, depth = await _judge_and_commit_matchup(state, hyp_a, hyp_b, index, ctx)
    return detail, depth, frozenset({hyp_a.id, hyp_b.id})


async def _execute_tournament_rounds(
    state: WorkflowState, tournament_rounds: int, ctx: _TournamentContext
) -> tuple[list[dict[str, Any]], int]:
    """Commit before selecting so matchmaking sees current ratings; distinct
    pair exhaustion ends work rather than replaying wins."""
    details: list[dict[str, Any]] = []
    total_llm_calls = 0
    judged: set[frozenset[str]] = set()
    for index in range(tournament_rounds):
        round_result = await _run_one_round(state, index, judged, ctx)
        if round_result is None:
            break
        detail, depth, committed = round_result
        judged.add(committed)
        details.append(detail)
        total_llm_calls += depth
    return details, total_llm_calls


def _filter_eligible_hypotheses(
    hypotheses: list[Hypothesis],
) -> list[Hypothesis]:
    eligible = [hypothesis for hypothesis in hypotheses if hypothesis.is_rankable()]
    logger.info(
        "Ranking tournament: %s of %s hypotheses are rankable",
        len(eligible),
        len(hypotheses),
    )
    return eligible


def _unrankable_reasons(hypotheses: list[Hypothesis]) -> str:
    """Explain review exclusions, not verification demotion: undermined ideas
    still compete and cannot account for a thin rankable pool."""
    blocked = sum(1 for h in hypotheses if h.review_disposition in BLOCKING_REVIEW_DISPOSITIONS)
    return f"{blocked} rejected in review"


async def _run_tournament(
    state: WorkflowState,
    hypotheses: list[Hypothesis],
    eligible: list[Hypothesis],
) -> dict[str, Any]:
    tournament_rounds, guidance = await prepare_ranking_round(state, eligible)

    prompt = prepare_ranking_prompt_context(state, preferences=state.get("preferences"))
    ctx = _TournamentContext(
        eligible,
        state["research_goal"],
        state.get("current_iteration", 0),
        replace(prompt, guidance=guidance),
    )
    matchup_details, total_llm_calls = await _execute_tournament_rounds(
        state, tournament_rounds, ctx
    )

    return await finalize_ranking(
        state, hypotheses, matchup_details, tournament_rounds, total_llm_calls
    )


async def ranking_node(state: WorkflowState) -> dict[str, Any]:
    """Stable goal/iteration seeds preserve deterministic pairings."""
    hypotheses = state["hypotheses"]
    eligible = _filter_eligible_hypotheses(hypotheses)

    if len(eligible) < 2:
        logger.warning(
            "Tournament skipped: %s of %s hypotheses are rankable (%s)",
            len(eligible),
            len(hypotheses),
            _unrankable_reasons(hypotheses),
        )
        return {"hypotheses": hypotheses}

    # The scheduler runs ranking each cycle; tournament_pairs must be consumed
    # as a whole-run budget rather than repurchased per invocation.
    if remaining_ranking_rounds(state, hypotheses) < 1:
        logger.info("Tournament budget spent for this run; skipping")
        return {"hypotheses": hypotheses}

    return await _run_tournament(state, hypotheses, eligible)


_build_tournament_pairings = build_tournament_pairings
