"""Operations mutate caller-owned ideas without store writes; guidance and
debating leaders are snapshots at the caller's tournament/wave boundary."""

from dataclasses import dataclass
from typing import Any

from co_scientist.core.constants import SINGLE_TURN_DEBATE_TURNS
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science.ranking.ranking_debate import (
    _RANKING_DEBATE_MAX_TURNS,
    _DebateContext,
    build_matchup,
    judge_matchup,
)
from co_scientist.science.ranking.ranking_lifecycle import (
    TournamentGuidance,
    _gather_tournament_context,
)
from co_scientist.science.scheduling.tournament import (
    DEFAULT_FINALISTS,
    finalist_count,
    ranked_leaders,
)


@dataclass(frozen=True)
class RankingPromptContext:
    research_goal: str
    model_name: str
    guidance: TournamentGuidance
    run_id: str | None = None
    criteria: list[str] | None = None
    preferences: str | None = None
    finalists: int = DEFAULT_FINALISTS


@dataclass(frozen=True)
class RankingJudgingContext:
    prompt: RankingPromptContext
    debate_ids: frozenset[str]


@dataclass(frozen=True)
class RankingJudgement:
    winner: str
    response: dict[str, Any]
    budgeted_turns: int


@dataclass(frozen=True)
class RankingMatchResult:
    detail: dict[str, Any]
    llm_calls: int


def prepare_ranking_prompt_context(
    state: WorkflowState, *, preferences: str | None = None
) -> RankingPromptContext:
    """Node tournaments supply scientist preferences; durable waves omit
    them."""
    return RankingPromptContext(
        research_goal=state["research_goal"],
        model_name=state["model_name"],
        guidance=_gather_tournament_context(state),
        run_id=state.get("run_id"),
        criteria=state.get("criteria"),
        preferences=preferences,
        finalists=finalist_count(state),
    )


def prepare_ranking_judging_context(
    prompt: RankingPromptContext, hypotheses: list[Hypothesis]
) -> RankingJudgingContext:
    # Sibling results cannot promote a newcomer within the parallel wave.
    leaders = ranked_leaders(hypotheses, prompt.finalists)
    return RankingJudgingContext(prompt, frozenset(h.id for h in leaders))


async def judge_ranking_matchup(
    pair: tuple[Hypothesis, Hypothesis],
    context: RankingJudgingContext,
    matchup_index: int,
) -> RankingJudgement:
    prompt = context.prompt
    guidance = prompt.guidance
    depth = (
        _RANKING_DEBATE_MAX_TURNS
        if all(hypothesis.id in context.debate_ids for hypothesis in pair)
        else SINGLE_TURN_DEBATE_TURNS
    )
    debate = _DebateContext(
        pair[0],
        pair[1],
        prompt.research_goal,
        prompt.model_name,
        supervisor_guidance=guidance.supervisor_guidance,
        meta_review=guidance.meta_review,
        tool_registry=guidance.tool_registry,
        run_setup_guidance=guidance.run_setup_guidance,
        run_focus_guidance=guidance.run_focus_guidance,
        run_id=prompt.run_id,
        matchup_index=matchup_index,
        criteria=prompt.criteria,
        preferences=prompt.preferences,
    )
    winner, response = await judge_matchup(debate, debate_turns=depth)
    return RankingJudgement(winner, response, depth)


def apply_ranking_matchup(
    pair: tuple[Hypothesis, Hypothesis],
    judgement: RankingJudgement,
    *,
    k_factor: int,
    current_iteration: int,
) -> RankingMatchResult:
    detail = build_matchup(
        pair,
        judgement.winner,
        judgement.response,
        k_factor=k_factor,
        iteration=current_iteration,
    ).to_dict()
    calls = int(judgement.response.get("debate_turns", judgement.budgeted_turns))
    return RankingMatchResult(detail, calls)
