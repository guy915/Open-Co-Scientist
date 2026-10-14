import dataclasses
import json
import logging
from typing import Any

from co_scientist.core.constants import (
    PROGRESS_META_REVIEW_COMPLETE,
    PROGRESS_META_REVIEW_START,
    THINKING_MAX_TOKENS,
    truncate,
)
from co_scientist.domains.research_state.models import (
    Hypothesis,
    MetricDeltas,
    create_metrics_update,
    phase_message,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.domains.safety.monitor import monitor_research_direction
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
)
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.node_degradation import run_or_degrade
from co_scientist.science.prompts import PromptRunContext, get_meta_review_prompt
from co_scientist.science.review_summary import mature_review_summary

logger = logging.getLogger(__name__)


def normalize_recurring_themes(
    recurring_themes: list[Any],
) -> list[dict[str, Any]]:
    return [_normalize_theme(entry) for entry in recurring_themes]


def _normalize_theme(entry: Any) -> dict[str, Any]:
    """Lax-provider bare strings still contain reportable feedback; retain
    their text."""
    if not isinstance(entry, dict):
        return _theme_node(str(entry), "", "", [])
    return _theme_node(
        str(entry.get("theme", "")),
        str(entry.get("description", "")),
        # json_object providers can return integer frequency despite a string
        # schema.
        str(entry.get("frequency", "")),
        _normalize_sub_themes(entry.get("sub_themes")),
    )


def _theme_node(
    theme: str,
    description: str,
    frequency: str,
    sub_themes: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "theme": theme,
        "description": description,
        "frequency": frequency,
        "sub_themes": sub_themes,
    }


def _normalize_sub_themes(sub_themes: Any) -> list[dict[str, Any]]:
    if not isinstance(sub_themes, list):
        return []
    return [_normalize_sub_theme(entry) for entry in sub_themes]


def _normalize_sub_theme(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {"theme": str(entry), "description": "", "points": []}
    return {
        "theme": str(entry.get("theme", "")),
        "description": str(entry.get("description", "")),
        "points": _normalize_points(entry.get("points")),
    }


def _normalize_points(points: Any) -> list[str]:
    if not isinstance(points, list):
        return []
    return [str(point) for point in points]


async def meta_review_node(state: WorkflowState) -> dict[str, Any]:
    hypotheses = state["hypotheses"]
    logger.info("Synthesizing meta-review from %s hypotheses", len(hypotheses))
    return await _run_meta_review_phase(state, hypotheses)


async def _run_meta_review_phase(
    state: WorkflowState, hypotheses: list[Hypothesis]
) -> dict[str, Any]:
    """Later reviews must not erase earlier feedback failure patterns."""
    await emit_progress(
        state,
        "meta_review_start",
        "Synthesizing insights from all reviews...",
        PROGRESS_META_REVIEW_START,
    )

    all_reviews = _collect_feedback_records(hypotheses, state.get("tournament_matchups", []))
    if not all_reviews:
        logger.warning("No reviews available for meta-review")
        return _empty_meta_review_result()

    # Exhausted durable synthesis attempts would settle the run failed and lose
    # its report; degrade the enhancement instead.
    return await run_or_degrade(
        state,
        lambda: _synthesize_and_monitor(state, all_reviews),
        schema_name="meta_review",
        fallback=_degraded_meta_review_result,
        lost="evolution and ranking continue without cross-hypothesis guidance for this cycle",
    )


async def _synthesize_and_monitor(
    state: WorkflowState,
    all_reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    meta_review = await _synthesize_meta_review(state, all_reviews)

    await emit_progress(
        state,
        "meta_review_complete",
        "Meta-review synthesis complete",
        PROGRESS_META_REVIEW_COMPLETE,
        strengths_count=len(meta_review["common_strengths"]),
        recommendations_count=len(meta_review["strategic_recommendations"]),
    )

    # Screen inside degradation: deterministic safety policy needs no call, and
    # failed synthesis supplies no direction that could trigger a hold.
    halt = await monitor_research_direction(state, meta_review)
    return {**_build_meta_review_result(meta_review), **halt}


async def _synthesize_meta_review(
    state: WorkflowState,
    all_reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    prompt, schema = get_meta_review_prompt(
        research_goal=state["research_goal"],
        all_reviews=json.dumps(all_reviews, indent=2),
        preferences=state.get("preferences"),
        context=PromptRunContext(
            supervisor_guidance=state.get("supervisor_guidance"),
            tool_registry=state.get("tool_registry"),
            run_setup_guidance=state.get("run_setup_guidance"),
            run_focus_guidance=state.get("run_focus_guidance"),
        ),
    )
    # Supervisor-model synthesis steers cross-hypothesis evolution.
    response = await call_llm_json(
        prompt=prompt,
        spec=CompletionSpec(
            role="meta_review",
            model_name=state["supervisor_model_name"],
            max_tokens=THINKING_MAX_TOKENS,
            json_schema=schema,
        ),
        options=LLMCallOptions(
            run_id=state.get("run_id"),
            prompt_name="meta_review",
        ),
    )
    meta_review = _build_meta_review(response)
    # One combined log record avoids competing SQLite writes and crowding
    # the reader's bounded event window.
    logger.info(
        "Meta-review complete: %s common strengths, %s strategic recommendations",
        len(meta_review["common_strengths"]),
        len(meta_review["strategic_recommendations"]),
    )
    return meta_review


def _build_meta_review_result(meta_review: dict[str, Any]) -> dict[str, Any]:
    metrics = create_metrics_update(deltas=MetricDeltas(llm_calls=1))

    return {
        "meta_review": meta_review,
        "metrics": metrics,
        "messages": phase_message(
            "meta_review",
            "Synthesized meta-review from all hypotheses",
            themes=len(meta_review.get("emerging_themes", [])),
        ),
    }


def _empty_meta_review_result() -> dict[str, Any]:
    return {
        "meta_review": {
            "summary": "No reviews available",
            "common_strengths": [],
            "common_weaknesses": [],
            "strategic_recommendations": [],
        }
    }


def _degraded_meta_review_result() -> dict[str, Any]:
    """Provider failure is not "no reviews"; the report must distinguish
    unavailable synthesis from genuinely absent input."""
    result = _empty_meta_review_result()
    result["meta_review"]["summary"] = "Meta-review synthesis was unavailable for this cycle"
    return result


def _collect_review_summaries(
    hypotheses: list[Hypothesis],
) -> list[dict[str, Any]]:
    """Keep chronological feedback and one-based labels: the model quotes
    hypothesis numbers in scientist-facing recommendations."""
    all_reviews = []
    for i, hyp in enumerate(hypotheses, start=1):
        if not hyp.reviews:
            continue

        review_data = {
            "record_type": "review_history",
            "hypothesis_index": i,
            "hypothesis_text": truncate(hyp.text),
            "reviews": [dataclasses.asdict(review) for review in hyp.reviews],
            "elo_rating": hyp.elo_rating,
            "win_loss_record": f"{hyp.win_count}W-{hyp.loss_count}L",
            "deep_verification_verdict": hyp.deep_verification_verdict,
        }
        mature_reviews = mature_review_summary(hyp.enrichments)
        if mature_reviews is not None:
            review_data["mature_reviews"] = mature_reviews
        all_reviews.append(review_data)
    return all_reviews


def _collect_debate_records(
    matchups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Match numbers are one-based because the model may quote them to the
    scientist."""
    return [
        {
            "record_type": "ranking_debate",
            "match_index": index,
            "hypothesis_a_id": matchup.get("hypothesis_a_id"),
            "hypothesis_b_id": matchup.get("hypothesis_b_id"),
            "hypothesis_a": matchup.get("hypothesis_a"),
            "hypothesis_b": matchup.get("hypothesis_b"),
            "winner_id": matchup.get("winner_id"),
            "winner": matchup.get("winner"),
            "reasoning": matchup.get("reasoning"),
            "confidence": matchup.get("confidence"),
            "debate_turns": matchup.get("debate_turns", 1),
            "debate_transcript": matchup.get("debate_transcript", []),
        }
        for index, matchup in enumerate(matchups, start=1)
    ]


def _collect_feedback_records(
    hypotheses: list[Hypothesis],
    matchups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        *_collect_review_summaries(hypotheses),
        *_collect_debate_records(matchups),
    ]


def _build_meta_review(response: dict[str, Any]) -> dict[str, Any]:
    """This shape steers ranking, evolution and all generation strategies;
    periodic open-direction feedback needs no extra call."""
    # Prompt/safety readers need only top-level area names; repeated nested
    # depth wastes per-cycle context. The report retains the full taxonomy.
    recurring_themes = normalize_recurring_themes(response.get("recurring_themes", []))
    emerging_themes = [theme["theme"] for theme in recurring_themes]

    return {
        "summary": response.get("meta_review_summary", ""),
        "common_strengths": response.get("strengths", []),
        "common_weaknesses": response.get("weaknesses", []),
        "emerging_themes": emerging_themes,
        "recurring_themes": recurring_themes,
        "strategic_recommendations": response.get("strategic_recommendations", []),
        "potential_connections": response.get("potential_connections", []),
        "candidate_comparison": response.get("candidate_comparison", {}),
        "existing_solutions_comparison": response.get("existing_solutions_comparison", {}),
        "main_research_directions": response.get("main_research_directions", ""),
    }
