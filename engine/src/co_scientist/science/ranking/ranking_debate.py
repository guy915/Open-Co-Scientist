import asyncio
import dataclasses
import hashlib
import logging
import re
import weakref
from typing import Any, Final, NamedTuple

from co_scientist.core.constants import (
    ELO_K_ANNEALED_MINIMUM,
    ELO_K_ANNEALING_HALF_LIFE,
    ELO_K_FACTOR,
    ELO_MARGIN_MULTIPLIER_CAP,
    ELO_MARGIN_VICTORY_SCALE,
    ELO_UPSET_MARGIN,
    LOW_TEMPERATURE,
    RANKING_WAVE_MIN_SIZE,
    RANKING_WAVE_SIZE,
    SINGLE_TURN_DEBATE_TURNS,
    THINKING_MAX_TOKENS,
    truncate,
)
from co_scientist.core.prompt_layout import append_item_context
from co_scientist.domains.research_state.models import (
    ExecutionMetrics,
    Hypothesis,
    MetricDeltas,
    create_metrics_update,
    phase_message,
)
from co_scientist.domains.research_state.models.matchup import (
    Matchup,
    presented_first,
    verdict_number,
)
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
    indexed_prompt_name,
    record_deterministic_fallback,
)
from co_scientist.science.prompts import (
    PromptRunContext,
    RankingSide,
    get_ranking_prompt,
)
from co_scientist.science.review_summary import mature_review_summary
from co_scientist.science.schemas.review import RANKING_COMPARISON_CRITERIA

logger = logging.getLogger(__name__)


def _review_summary(hypothesis: Hypothesis) -> dict[str, Any] | None:
    """Quadratic matchup volume needs numeric review context without repeated
    prose."""
    summary = hypothesis.review_summary()
    if summary is None:
        return None
    return {
        "scores": summary["scores"],
        "overall_score": summary["overall_score"],
    }


def _ranking_side(hypothesis: Hypothesis) -> RankingSide:
    return RankingSide(
        text=hypothesis.text,
        review=_review_summary(hypothesis),
        reflection_notes=hypothesis.reflection_notes,
        deep_verification=hypothesis.deep_verification_summary(),
        mature_reviews=mature_review_summary(hypothesis.enrichments),
    )


# SSR Note 9.3 specifies typical 3-5 turns, at most 10. The loop and follow-up
# prompt must share this envelope to avoid stale pacing instructions.
_RANKING_DEBATE_TYPICAL_MIN_TURNS: Final = 3


_RANKING_DEBATE_TYPICAL_MAX_TURNS: Final = 5


_RANKING_DEBATE_MAX_TURNS: Final = 10


@dataclasses.dataclass(frozen=True)
class _MatchupPrompt:
    prompt: str
    schema: dict[str, Any] | None


@dataclasses.dataclass(frozen=True)
class _DebateRun:
    """Presentation-independent base/fallback/parity stay fixed while turns
    accumulate."""

    base: _MatchupPrompt
    transcript: list[dict[str, Any]]
    fallback: str
    start_parity: int


# The judge protocol ends with a literal verdict; quoted format examples must
# not count as decisions.
_VERDICT_LINE_RE = re.compile(
    r"better\s+(?:idea|hypothesis)\s*:\s*([12ab])(?![\w])",
    re.IGNORECASE,
)


def _parse_verdict_line(text: str) -> str | None:
    """The concluding verdict wins; quoted "1 or 2" is a format example, not
    a decision."""
    for match in reversed(list(_VERDICT_LINE_RE.finditer(text or ""))):
        if re.match(r"\s*or\b", text[match.end() :], re.IGNORECASE):
            continue
        token = match.group(1).lower()
        return {"1": "a", "2": "b"}.get(token, token)
    return None


def _parse_matchup_winner(response: dict[str, Any], *, fallback: str) -> tuple[str, bool]:
    """Prefer prose verdicts, with JSON winner for offline responses; invalid
    judgments use a position-balanced fallback."""
    verdict = _parse_verdict_line(str(response.get("decision_summary") or ""))
    if verdict is not None:
        return verdict, True
    winner = str(response.get("winner") or "").lower()
    if winner not in ["a", "b"]:
        logger.warning("Invalid winner '%s'; using balanced fallback", winner)
        return fallback, False
    return winner, True


def _balanced_invalid_fallback(
    hypothesis_a: Hypothesis,
    hypothesis_b: Hypothesis,
    matchup_index: int | None,
) -> str:
    identity = "|".join(sorted((hypothesis_a.id, hypothesis_b.id)))
    base = int(hashlib.sha256(identity.encode()).hexdigest()[:2], 16) % 2
    parity = base ^ int(matchup_index or 0) % 2
    chosen_id = sorted((hypothesis_a.id, hypothesis_b.id))[parity]
    return "a" if chosen_id == hypothesis_a.id else "b"


def _presented_number(entry: dict[str, Any], swapped: bool) -> str:
    """Turn prompts name numbered sides, not UUIDs; canonical letters become
    misleading when presentation swaps."""
    return "1" if (entry["winner"] == "a") != swapped else "2"


def _prior_turn_order_note(entry: dict[str, Any], swapped: bool) -> str:
    """Relabeling votes does not relabel quoted prose; state old presentation
    order so the judge does not mistake swapped labels for contradiction."""
    if (presented_first(entry) == "2") != swapped:
        return (
            " (that turn presented the two hypotheses in the opposite "
            "order to this turn, so where its text says 'hypothesis 1' it "
            "means this turn's hypothesis 2, and vice versa)"
        )
    return " (that turn presented them in the same order as this turn)"


def _append_debate_context(
    prompt: str, transcript: list[dict[str, Any]], *, swapped: bool = False
) -> str:
    """Only follow-up turns can re-examine prior reasoning."""
    lines = ["\n\n## Prior Debate Turns (re-examine and refine)\n"]
    for entry in transcript:
        lines.append(
            f"- Turn {entry['turn']} favored hypothesis "
            f"{_presented_number(entry, swapped)}"
            f"{_prior_turn_order_note(entry, swapped)}: "
            f"{entry['reasoning']}\n"
        )
    lines.append(
        "\nPose clarifying questions to address any ambiguities or "
        "uncertainties. This debate typically settles in "
        f"{_RANKING_DEBATE_TYPICAL_MIN_TURNS}-"
        f"{_RANKING_DEBATE_TYPICAL_MAX_TURNS} turns and never runs past "
        f"{_RANKING_DEBATE_MAX_TURNS}. If the turns above already "
        "establish a clear preference that does not depend on which "
        "hypothesis was presented first, confirm that verdict decisively; "
        "otherwise challenge the weak arguments before deciding.\n"
    )
    return append_item_context(prompt, "".join(lines))


class _DebateContext(NamedTuple):
    hypothesis_a: Hypothesis
    hypothesis_b: Hypothesis
    research_goal: str
    model_name: str
    supervisor_guidance: dict[str, Any] | None = None
    meta_review: dict[str, Any] | None = None
    tool_registry: Any | None = None
    run_setup_guidance: str | None = None
    run_focus_guidance: str | None = None
    run_id: str | None = None
    matchup_index: int | None = None
    criteria: list[str] | None = None
    preferences: str | None = None
    debate: bool = False


def _render_ordered_prompt(
    hypothesis_a: Hypothesis,
    hypothesis_b: Hypothesis,
    ctx: _DebateContext,
) -> _MatchupPrompt:
    prompt, schema = get_ranking_prompt(
        research_goal=ctx.research_goal,
        side_a=_ranking_side(hypothesis_a),
        side_b=_ranking_side(hypothesis_b),
        context=PromptRunContext(
            supervisor_guidance=ctx.supervisor_guidance,
            meta_review=ctx.meta_review,
            tool_registry=ctx.tool_registry,
            run_setup_guidance=ctx.run_setup_guidance,
            run_focus_guidance=ctx.run_focus_guidance,
            preferences=ctx.preferences,
            criteria=ctx.criteria,
        ),
        debate=ctx.debate,
    )
    return _MatchupPrompt(prompt, schema)


def _build_turn_prompt(
    ctx: _DebateContext,
    turn: int,
    swapped: bool,
    base: _MatchupPrompt,
    transcript: list[dict[str, Any]],
) -> _MatchupPrompt:
    if swapped:
        turn_prompt = _render_ordered_prompt(ctx.hypothesis_b, ctx.hypothesis_a, ctx)
    else:
        turn_prompt = base
    if turn > 0:
        turn_prompt = dataclasses.replace(
            turn_prompt,
            prompt=_append_debate_context(turn_prompt.prompt, transcript, swapped=swapped),
        )
    return turn_prompt


def _ranking_debate_consensus(votes: list[str], turns_run: int, turn_budget: int) -> bool:
    """Consecutive agreement spans opposite presentation orders, reducing
    position bias; require a real exchange before accepting it."""
    if turns_run >= min(turn_budget, _RANKING_DEBATE_MAX_TURNS):
        return True
    if turns_run < min(_RANKING_DEBATE_TYPICAL_MIN_TURNS, turn_budget):
        return False
    return len(votes) >= 2 and votes[-1] == votes[-2]


def _resolve_turn_winner(
    response: dict[str, Any], swapped: bool, fallback: str
) -> tuple[str, bool]:
    raw_fallback = ("b" if fallback == "a" else "a") if swapped else fallback
    raw_winner, valid_output = _parse_matchup_winner(response, fallback=raw_fallback)
    winner = ("b" if raw_winner == "a" else "a") if swapped else raw_winner
    return winner, valid_output


def _finalize_debate_response(
    response: dict[str, Any],
    votes: list[str],
    run: _DebateRun,
    model_name: str,
) -> str:
    """Persist and bill actual judged turns, not offered depth: consensus can
    stop early."""
    turns = len(votes)
    winner = "a" if votes.count("a") > votes.count("b") else "b"
    if votes.count("a") == votes.count("b"):
        winner = run.fallback
        record_deterministic_fallback(model_name, "ranking_tied_votes")

    response["debate_turns"] = turns
    response["debate_transcript"] = run.transcript
    response["debate_verdict"] = verdict_number(winner)
    response["judge_model"] = model_name
    response["consensus_votes"] = votes
    response["position_balanced"] = turns > 1
    response["invalid_output_fallback"] = not all(turn["valid_output"] for turn in run.transcript)
    return winner


# Confidence proxies the unreported victory margin; unknown levels imply none.
_CONFIDENCE_MARGINS = {"high": 1.0, "medium": 0.5}


def annealed_k_factor(
    base_k_factor: int,
    matches_played: int,
    half_life: int | None = None,
) -> int:
    """Per-idea match history calibrates K; a floor prevents frozen ratings.
    Nonpositive half-life retains fixed-K historical behavior."""
    if half_life is None:
        half_life = ELO_K_ANNEALING_HALF_LIFE
    if half_life <= 0 or matches_played <= 0:
        return base_k_factor
    halvings = matches_played // half_life
    annealed = base_k_factor / (2**halvings)
    return max(ELO_K_ANNEALED_MINIMUM, int(annealed))


def margin_scaled_k_factor(
    base_k_factor: int,
    confidence: str | None,
    scale: float | None = None,
) -> int:
    """The judge supplies confidence rather than scores, so it proxies
    victory margin; nonpositive scale retains fixed-K historical behavior."""
    if scale is None:
        scale = ELO_MARGIN_VICTORY_SCALE
    if scale <= 0 or not confidence:
        return base_k_factor
    margin = _CONFIDENCE_MARGINS.get(confidence.strip().lower(), 0.0)
    if margin <= 0:
        return base_k_factor
    multiplier = min(ELO_MARGIN_MULTIPLIER_CAP, 1.0 + scale * margin)
    return int(base_k_factor * multiplier)


def effective_k_factor(
    base_k_factor: int,
    matches_played: int,
    confidence: str | None = None,
) -> int:
    """Anneal calibration first, then scale this verdict's decisiveness; both
    optional knobs default off to preserve historical ratings."""
    annealed = annealed_k_factor(base_k_factor, matches_played)
    return margin_scaled_k_factor(annealed, confidence)


def calculate_elo_update(
    winner_elo: int,
    loser_elo: int,
    k_factor: int = ELO_K_FACTOR,
    *,
    loser_k_factor: int | None = None,
) -> tuple[int, int]:
    loser_k = k_factor if loser_k_factor is None else loser_k_factor
    # Standard Elo uses the 400-point logistic scale: a 400-point gap means
    # tenfold win odds, and the expected probabilities sum to one.
    expected_winner = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    expected_loser = 1 / (1 + 10 ** ((winner_elo - loser_elo) / 400))

    new_winner_elo = winner_elo + k_factor * (1 - expected_winner)
    new_loser_elo = loser_elo + loser_k * (0 - expected_loser)

    return int(new_winner_elo), int(new_loser_elo)


def match_tier(winner_elo_before: int, loser_elo_before: int, confidence: str) -> str:
    # A sufficiently large pre-match rating gap is an upset regardless of the
    # judge's stated confidence.
    if loser_elo_before - winner_elo_before >= ELO_UPSET_MARGIN:
        return "upset"
    normalized = confidence.strip().lower()
    if normalized == "high":
        return "decisive"
    if normalized == "medium":
        return "clear"
    return "narrow"


def _extract_criteria_comparisons(
    response: dict[str, Any],
) -> dict[str, str]:
    """Keep only canonical aspects; undeclared keys are model invention, not
    evidence."""
    explanation = response.get("judgment_explanation")
    if not isinstance(explanation, dict):
        return {}
    return {
        name: str(explanation[name])
        for name in RANKING_COMPARISON_CRITERIA
        if explanation.get(name)
    }


def _extract_reasoning(response: dict[str, Any]) -> str:
    reasoning: str = response.get("decision_summary", "")
    if not reasoning and "judgment_explanation" in response:
        reasoning = " | ".join(
            f"{k}: {v}" for k, v in response["judgment_explanation"].items() if v
        )
    if not reasoning:
        reasoning = "No reasoning provided"
    return reasoning


def _compute_elo_update(
    winner_hyp: Hypothesis,
    loser_hyp: Hypothesis,
    k_factor: int | None,
    confidence: str | None = None,
) -> tuple[int, int]:
    base_k = k_factor if k_factor is not None else ELO_K_FACTOR
    winner_k = effective_k_factor(base_k, winner_hyp.total_matches, confidence)
    loser_k = effective_k_factor(base_k, loser_hyp.total_matches, confidence)
    new_winner_elo, new_loser_elo = calculate_elo_update(
        winner_elo=winner_hyp.elo_rating,
        loser_elo=loser_hyp.elo_rating,
        k_factor=winner_k,
        loser_k_factor=loser_k,
    )
    logger.debug(
        "Matchup result: Winner %s -> %s, Loser %s -> %s",
        winner_hyp.elo_rating,
        new_winner_elo,
        loser_hyp.elo_rating,
        new_loser_elo,
    )
    winner_hyp.elo_rating = new_winner_elo
    loser_hyp.elo_rating = new_loser_elo
    winner_hyp.win_count += 1
    loser_hyp.loss_count += 1
    return new_winner_elo, new_loser_elo


def build_matchup(
    pair: tuple[Hypothesis, Hypothesis],
    winner: str,
    response: dict[str, Any],
    *,
    k_factor: int | None,
    iteration: int,
) -> Matchup:
    hyp_a, hyp_b = pair
    winner_hyp, loser_hyp = pair if winner == "a" else (hyp_b, hyp_a)
    winner_before, loser_before = winner_hyp.elo_rating, loser_hyp.elo_rating
    winner_after, loser_after = _compute_elo_update(
        winner_hyp, loser_hyp, k_factor, response.get("confidence_level")
    )
    return Matchup(
        iteration=int(iteration),
        hypothesis_a=truncate(hyp_a.text),
        hypothesis_b=truncate(hyp_b.text),
        hypothesis_a_id=hyp_a.id,
        hypothesis_b_id=hyp_b.id,
        winner_id=winner_hyp.id,
        winner=winner,
        reasoning=_extract_reasoning(response),
        confidence=response.get("confidence_level", "Unknown"),
        tier=match_tier(winner_before, loser_before, response.get("confidence_level", "")),
        criteria_comparisons=_extract_criteria_comparisons(response),
        debate_turns=response.get("debate_turns", 1),
        debate_transcript=response.get("debate_transcript", []),
        debate_verdict=response.get("debate_verdict") or verdict_number(winner),
        judge_model=response.get("judge_model"),
        consensus_votes=response.get("consensus_votes", [winner]),
        position_balanced=response.get("position_balanced", False),
        invalid_output_fallback=response.get("invalid_output_fallback", False),
        winner_elo_before=winner_before,
        winner_elo_after=winner_after,
        loser_elo_before=loser_before,
        loser_elo_after=loser_after,
    )


def _ranking_metrics_update(matches_judged: int, total_llm_calls: int | None) -> ExecutionMetrics:
    """Bill actual matches, not offered rounds; count multi-turn calls
    separately so early exhaustion cannot charge nonexistent work."""
    llm_calls = total_llm_calls if total_llm_calls is not None else matches_judged
    metrics = create_metrics_update(
        deltas=MetricDeltas(llm_calls=llm_calls, tournaments=matches_judged)
    )
    logger.debug(
        "ranking node creating metrics delta: tournaments=%s, llm_calls=%s",
        matches_judged,
        llm_calls,
    )
    return metrics


def _build_ranking_delta(
    hypotheses: list[Hypothesis],
    matchup_details: list[dict[str, Any]],
    tournament_rounds: int,
    total_llm_calls: int | None = None,
) -> dict[str, Any]:
    matches_judged = len(matchup_details)
    metrics = _ranking_metrics_update(matches_judged, total_llm_calls)

    return {
        "hypotheses": hypotheses,
        "tournament_matchups": matchup_details,
        "metrics": metrics,
        "messages": phase_message(
            "ranking",
            f"Judged {matches_judged} of {tournament_rounds} tournament rounds",
            rounds=matches_judged,
            top_elo=hypotheses[0].elo_rating,
        ),
    }


# Asyncio guards belong to their loop; weak keys avoid cross-thread durable
# tasks sharing primitives or finished loops retaining them.
_ranking_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def effective_ranking_wave_size() -> int:
    """Throttle feedback narrows waves one-way within a process; reopening on
    a quiet wave would provoke the same account-level pressure again."""
    from co_scientist.platform.llm import rate_limited_attempt_count

    if rate_limited_attempt_count():
        return RANKING_WAVE_MIN_SIZE
    return RANKING_WAVE_SIZE


def _get_ranking_semaphore() -> asyncio.Semaphore:
    """A bound narrower than the wave silently serializes its calls,
    defeating the task's intended parallelism."""
    loop = asyncio.get_running_loop()
    semaphore = _ranking_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(RANKING_WAVE_SIZE)
        _ranking_semaphores[loop] = semaphore
    return semaphore


async def _call_matchup_judge(
    mp: _MatchupPrompt,
    ctx: _DebateContext,
) -> dict[str, Any]:
    prompt_name = indexed_prompt_name("ranking_matchup", ctx.matchup_index)
    async with _get_ranking_semaphore():
        # Quadratic matchup volume requires reasoning within the token budget.
        return await call_llm_json(
            prompt=mp.prompt,
            spec=CompletionSpec(
                role="ranking",
                model_name=ctx.model_name,
                max_tokens=THINKING_MAX_TOKENS,
                temperature=LOW_TEMPERATURE,
                json_schema=mp.schema,
            ),
            options=LLMCallOptions(
                run_id=ctx.run_id,
                prompt_name=prompt_name,
            ),
        )


async def _run_debate_turn(
    ctx: _DebateContext,
    turn: int,
    swapped: bool,
    mp: _MatchupPrompt,
    fallback: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    response = await _call_matchup_judge(mp, ctx)
    winner, valid_output = _resolve_turn_winner(response, swapped, fallback)
    if not valid_output:
        record_deterministic_fallback(ctx.model_name, "ranking_invalid_turn")
    entry = {
        "turn": turn + 1,
        "winner": winner,
        "winner_id": (ctx.hypothesis_a.id if winner == "a" else ctx.hypothesis_b.id),
        "reasoning": _extract_reasoning(response),
        "presentation_order": "ba" if swapped else "ab",
        "valid_output": valid_output,
    }
    return winner, entry, response


async def _execute_debate_turn(
    ctx: _DebateContext,
    turn: int,
    run: _DebateRun,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    swapped = (turn + run.start_parity) % 2 == 1
    turn_mp = _build_turn_prompt(ctx, turn, swapped, run.base, run.transcript)
    return await _run_debate_turn(ctx, turn, swapped, turn_mp, run.fallback)


async def _run_debate_turns(
    ctx: _DebateContext,
    turns: int,
    base: _MatchupPrompt,
    fallback: str,
) -> tuple[list[str], _DebateRun, dict[str, Any]]:
    """Turns must be serial: each judge re-examines the prior transcript."""
    run = _DebateRun(
        base=base,
        transcript=[],
        fallback=fallback,
        start_parity=int(ctx.matchup_index or 0) % 2,
    )
    votes: list[str] = []
    response: dict[str, Any] = {}
    for turn in range(turns):
        winner, entry, response = await _execute_debate_turn(ctx, turn, run)
        votes.append(winner)
        run.transcript.append(entry)
        if _ranking_debate_consensus(votes, turn + 1, turns):
            break
    return votes, run, response


async def judge_matchup(
    ctx: _DebateContext,
    debate_turns: int = SINGLE_TURN_DEBATE_TURNS,
) -> tuple[str, dict[str, Any]]:
    turns = max(SINGLE_TURN_DEBATE_TURNS, debate_turns)
    if turns > SINGLE_TURN_DEBATE_TURNS:
        turns = min(turns, _RANKING_DEBATE_MAX_TURNS)
    # Choose one prompt family per matchup so swapped re-renders keep the same
    # single-turn or scientific-debate contract.
    ctx = ctx._replace(debate=turns > SINGLE_TURN_DEBATE_TURNS)
    base = _render_ordered_prompt(ctx.hypothesis_a, ctx.hypothesis_b, ctx)
    fallback = _balanced_invalid_fallback(ctx.hypothesis_a, ctx.hypothesis_b, ctx.matchup_index)
    votes, run, response = await _run_debate_turns(ctx, turns, base, fallback)
    winner = _finalize_debate_response(response, votes, run, ctx.model_name)
    return winner, response
