import asyncio
import dataclasses
import logging
from collections.abc import Coroutine
from typing import Any

from co_scientist.core.constants import (
    COMPARATIVE_BATCH_THRESHOLD,
    EXTENDED_MAX_TOKENS,
    HIGH_TEMPERATURE,
    PROGRESS_REVIEW_COMPLETE,
    PROGRESS_REVIEW_START,
    REVIEW_BATCH_FREE_HYPOTHESES,
    REVIEW_BATCH_MAX_TOKENS_CAP,
    REVIEW_BATCH_TOKENS_PER_HYPOTHESIS,
    THINKING_MAX_TOKENS,
    scaled_max_tokens,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.models import (
    Hypothesis,
    HypothesisReview,
    MetricDeltas,
    create_metrics_update,
    has_peer_review,
    phase_message,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
    indexed_prompt_name,
)
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.prompts import (
    PromptRunContext,
    get_review_batch_prompt,
    get_review_prompt,
)
from co_scientist.science.reflection.review_gate import (
    apply_initial_review_gate,
)
from co_scientist.science.reflection.review_gate import (
    refresh_review_dispositions as refresh_review_dispositions,
)
from co_scientist.science.schemas.review import (
    REVIEW_SCORE_MAXIMUM,
    REVIEW_SCORE_MINIMUM,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ReviewContext:
    research_goal: str
    model_name: str
    run_id: str | None = None
    supervisor_guidance: dict[str, Any] | None = None
    meta_review: dict[str, Any] | None = None
    tool_registry: Any | None = None
    run_setup_guidance: str | None = None
    run_focus_guidance: str | None = None

    @classmethod
    def from_state(cls, state: WorkflowState) -> "ReviewContext":
        return cls(
            research_goal=state["research_goal"],
            model_name=state["model_name"],
            run_id=state.get("run_id"),
            supervisor_guidance=state.get("supervisor_guidance"),
            meta_review=state.get("meta_review"),
            tool_registry=state.get("tool_registry"),
            run_setup_guidance=state.get("run_setup_guidance"),
            run_focus_guidance=state.get("run_focus_guidance"),
        )


@dataclasses.dataclass(frozen=True)
class _BatchReviewCall:
    prompt: str
    schema: dict[str, Any] | None
    max_tokens: int
    max_attempts: int


def _sanitize_review_scores(scores: Any) -> dict[str, int]:
    """json_object may ignore schemas; drop invalid scores rather than clamp
    parse defects to the rubric floor."""
    if not isinstance(scores, dict):
        return {}
    sanitized: dict[str, int] = {}
    for name, value in scores.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            logger.debug("dropping non-numeric review score %s=%r", name, value)
            continue
        if not REVIEW_SCORE_MINIMUM <= value <= REVIEW_SCORE_MAXIMUM:
            logger.debug("dropping out-of-range review score %s=%r", name, value)
            continue
        sanitized[str(name)] = round(value)
    return sanitized


def _sanitize_novelty_list(value: Any) -> list[str]:
    """json_object providers may ignore schemas; malformed lists must degrade
    safely."""
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item).strip())]


def _review_from_response(data: dict[str, Any]) -> HypothesisReview:
    scores = _sanitize_review_scores(data.get("scores"))
    novelty_review = data.get("novelty_review")
    if not isinstance(novelty_review, dict):
        novelty_review = {}
    # Criterion-derived averages stay consistent with the scores shown to the scientist.
    overall_score = sum(scores.values()) / len(scores) if scores else data.get("overall_score", 0.0)

    return HypothesisReview(
        review_summary=data.get("review_summary", ""),
        scores=scores,
        safety_ethical_concerns=data.get("safety_ethical_concerns", ""),
        detailed_feedback=data.get("detailed_feedback", {}),
        constructive_feedback=data.get("constructive_feedback", ""),
        overall_score=overall_score,
        already_explored=_sanitize_novelty_list(novelty_review.get("already_explored")),
        novel_aspects=_sanitize_novelty_list(novelty_review.get("novel_aspects")),
    )


def _prepare_batch_review_call(
    hypotheses: list[Hypothesis],
    context: ReviewContext,
) -> _BatchReviewCall:
    prompt, schema = get_review_batch_prompt(
        research_goal=context.research_goal,
        hypotheses_list=_build_hypotheses_list_text(hypotheses),
        context=PromptRunContext(
            supervisor_guidance=context.supervisor_guidance,
            meta_review=context.meta_review,
            tool_registry=context.tool_registry,
            run_setup_guidance=context.run_setup_guidance,
            run_focus_guidance=context.run_focus_guidance,
        ),
    )
    hypothesis_count = len(hypotheses)
    max_tokens, max_attempts = _scaled_batch_review_budget(hypothesis_count)
    logger.debug(
        "batch review: %s hypotheses, max_tokens=%s",
        hypothesis_count,
        max_tokens,
    )
    return _BatchReviewCall(
        prompt=prompt,
        schema=schema,
        max_tokens=max_tokens,
        max_attempts=max_attempts,
    )


def _build_hypotheses_list_text(hypotheses: list[Hypothesis]) -> str:
    """Response hypothesis_index refers to these one-based labels, not array
    position."""
    return "\n\n".join(
        [f"**Hypothesis {number}:**\n{hyp.text}" for number, hyp in enumerate(hypotheses, start=1)]
    )


def _scaled_batch_review_budget(hypothesis_count: int) -> tuple[int, int]:
    max_tokens = scaled_max_tokens(
        THINKING_MAX_TOKENS,
        hypothesis_count,
        per_item=REVIEW_BATCH_TOKENS_PER_HYPOTHESIS,
        cap=REVIEW_BATCH_MAX_TOKENS_CAP,
        free_count=REVIEW_BATCH_FREE_HYPOTHESES,
    )
    max_attempts = 7 if hypothesis_count > 10 else 5
    return max_tokens, max_attempts


def _log_batch_review_response_shape(
    response: dict[str, Any],
    reviews_data: list[Any],
    hypotheses: list[Hypothesis],
    run_id: str | None,
) -> None:
    logger.info("Batch review response keys: %s", list(response.keys()))
    logger.info(
        "Reviews data type: %s, length: %s",
        type(reviews_data),
        len(reviews_data) if isinstance(reviews_data, list) else "N/A",
    )
    logger.info("Expected %s reviews, received %s", len(hypotheses), len(reviews_data))

    if len(reviews_data) != len(hypotheses):
        logger.error(
            "MISMATCH: Expected %s reviews but got %s. "
            "This indicates the LLM may have hit output token limits"
            " or failed to generate all reviews. "
            "Inspect review_batch call diagnostics for run %s",
            len(hypotheses),
            len(reviews_data),
            run_id,
        )


def _match_batch_entries_to_hypotheses(
    reviews_data: list[Any],
    hypothesis_count: int,
) -> list[Any]:
    """Valid one-based indices outrank response order; bad or duplicate
    indices fill only unmatched slots."""
    slots: list[Any] = [None] * hypothesis_count
    unplaced: list[Any] = []
    claimed: set[int] = set()
    for entry in reviews_data:
        number = entry.get("hypothesis_index") if isinstance(entry, dict) else None
        if (
            isinstance(number, int)
            and not isinstance(number, bool)
            and 1 <= number <= hypothesis_count
            and number not in claimed
        ):
            slots[number - 1] = entry
            claimed.add(number)
        else:
            unplaced.append(entry)

    remaining = iter(unplaced)
    for index in range(hypothesis_count):
        if slots[index] is None:
            slots[index] = next(remaining, None)
    return slots


def _convert_matched_entry(entry: Any, position: int) -> HypothesisReview | None:
    """A malformed entry must not abort valid peers; leave it unreviewed for
    retry."""
    if entry is None:
        logger.error("No review data for hypothesis %s", position)
        return None
    try:
        return _review_from_response(entry)
    except (AttributeError, KeyError, TypeError, ValueError):
        logger.warning(
            "Malformed review entry for hypothesis %s; recording it as"
            " a failed review instead of aborting the batch",
            position,
        )
        return None


def _parse_batch_review_response(
    response: dict[str, Any],
    hypotheses: list[Hypothesis],
    run_id: str | None,
) -> list[HypothesisReview | None]:
    reviews_data = response.get("reviews", [])
    _log_batch_review_response_shape(response, reviews_data, hypotheses, run_id)
    matched = _match_batch_entries_to_hypotheses(reviews_data, len(hypotheses))
    return [
        _convert_matched_entry(entry, position) for position, entry in enumerate(matched, start=1)
    ]


def _select_review_strategy(num_hypotheses: int) -> tuple[bool, str]:
    """Comparative scoring differentiates peers but shares a response
    ceiling; large pools need independent bounded calls."""
    use_comparative = num_hypotheses <= COMPARATIVE_BATCH_THRESHOLD
    if use_comparative:
        logger.info(
            "Reviewing %s hypotheses via comparative batch (≤%s)",
            num_hypotheses,
            COMPARATIVE_BATCH_THRESHOLD,
        )
        strategy_name = "comparative batch"
    else:
        logger.info(
            "Reviewing %s hypotheses via parallel individual (>%s)",
            num_hypotheses,
            COMPARATIVE_BATCH_THRESHOLD,
        )
        strategy_name = "parallel"
    return use_comparative, strategy_name


def _split_reviews_by_result(
    hypotheses: list[Hypothesis],
    reviews: list[HypothesisReview | None],
) -> tuple[list[tuple[Hypothesis, HypothesisReview]], int]:
    """Failures stay unreviewed for retry rather than entering ranking on
    zero-score placeholders."""
    pairs = [
        (hypothesis, review)
        for hypothesis, review in zip(hypotheses, reviews, strict=True)
        if review is not None
    ]
    return pairs, len(hypotheses) - len(pairs)


def _attach_reviews_to_hypotheses(
    hypotheses: list[Hypothesis],
    reviews: list[HypothesisReview],
) -> None:
    for hypothesis, review in zip(hypotheses, reviews, strict=True):
        hypothesis.reviews.append(review)
        hypothesis.score = review.overall_score


_apply_initial_review_gate = apply_initial_review_gate


async def review_single_hypothesis(
    hypothesis_text: str,
    context: ReviewContext,
    hypothesis_index: int | None = None,
) -> HypothesisReview:
    prompt, schema = get_review_prompt(
        research_goal=context.research_goal,
        hypothesis_text=hypothesis_text,
        context=PromptRunContext(
            supervisor_guidance=context.supervisor_guidance,
            meta_review=context.meta_review,
            tool_registry=context.tool_registry,
            run_setup_guidance=context.run_setup_guidance,
            run_focus_guidance=context.run_focus_guidance,
        ),
    )
    response = await _call_review_llm(
        prompt, schema, context.model_name, context.run_id, hypothesis_index
    )
    return _review_from_response(response)


async def _call_review_llm(
    prompt: str,
    schema: dict[str, Any] | None,
    model_name: str,
    run_id: str | None,
    hypothesis_index: int | None,
) -> dict[str, Any]:
    """Gathered failures leave peer reviews running."""
    return await call_llm_json(
        prompt=prompt,
        spec=CompletionSpec(
            role="review",
            model_name=model_name,
            max_tokens=EXTENDED_MAX_TOKENS,
            temperature=HIGH_TEMPERATURE,
            json_schema=schema,
        ),
        options=LLMCallOptions(
            run_id=run_id,
            prompt_name=indexed_prompt_name("review_individual", hypothesis_index),
        ),
    )


async def review_parallel_individual(
    hypotheses: list[Hypothesis],
    context: ReviewContext,
) -> list[HypothesisReview | None]:
    review_tasks = _build_parallel_review_tasks(hypotheses, context)
    results = await asyncio.gather(*review_tasks, return_exceptions=True)
    return [_individual_review_result(result, index) for index, result in enumerate(results)]


def _individual_review_result(
    result: HypothesisReview | BaseException,
    hypothesis_index: int,
) -> HypothesisReview | None:
    """gather returns control-flow errors as values; re-raise them so the
    worker can park or end the run."""
    if isinstance(result, TASK_CONTROL_FLOW_ERRORS):
        raise result
    if isinstance(result, BaseException):
        logger.warning("Review failed for hypothesis %s: %s", hypothesis_index, result)
        return None
    return result


def _build_parallel_review_tasks(
    hypotheses: list[Hypothesis],
    context: ReviewContext,
) -> list[Coroutine[Any, Any, HypothesisReview]]:
    return [
        review_single_hypothesis(
            hypothesis_text=hyp.text,
            context=context,
            hypothesis_index=i,
        )
        for i, hyp in enumerate(hypotheses)
    ]


async def review_comparative_batch(
    hypotheses: list[Hypothesis],
    context: ReviewContext,
) -> list[HypothesisReview | None]:
    """Relative scoring shares a token budget; malformed entries fail alone."""
    response = await _run_batch_review_call(hypotheses, context)
    return _parse_batch_review_response(response, hypotheses, context.run_id)


async def _run_batch_review_call(
    hypotheses: list[Hypothesis],
    context: ReviewContext,
) -> dict[str, Any]:
    call = _prepare_batch_review_call(hypotheses, context)
    return await _call_batch_review_llm(call, context)


async def _call_batch_review_llm(
    call: _BatchReviewCall,
    context: ReviewContext,
) -> dict[str, Any]:
    return await call_llm_json(
        prompt=call.prompt,
        spec=CompletionSpec(
            role="review",
            model_name=context.model_name,
            max_tokens=call.max_tokens,
            temperature=HIGH_TEMPERATURE,
            json_schema=call.schema,
        ),
        max_attempts=call.max_attempts,
        options=LLMCallOptions(
            run_id=context.run_id,
            prompt_name="review_batch",
        ),
    )


async def _run_review_strategy(
    state: WorkflowState,
    hypotheses: list[Hypothesis],
    use_comparative: bool,
) -> tuple[list[HypothesisReview | None], int]:
    context = ReviewContext.from_state(state)
    if use_comparative:
        return await review_comparative_batch(hypotheses, context), 1
    return (
        await review_parallel_individual(hypotheses, context),
        len(hypotheses),
    )


async def review_node(state: WorkflowState) -> dict[str, Any]:
    """Review only new children to avoid quadratic spend. Refresh
    dispositions before returning to honor later verdicts."""
    hypotheses = state["hypotheses"]
    refresh_review_dispositions(hypotheses, state.get("criteria"))
    unreviewed = [hyp for hyp in hypotheses if not has_peer_review(hyp)]
    _log_review_intake(hypotheses, unreviewed)

    if not unreviewed:
        return _skipped_review_result(hypotheses)

    reviews, llm_calls, strategy_name, failed_count = await _run_review_phase(state, unreviewed)

    return _review_node_result(hypotheses, reviews, llm_calls, strategy_name, failed_count)


async def _run_review_phase(
    state: WorkflowState, unreviewed: list[Hypothesis]
) -> tuple[list[HypothesisReview], int, str, int]:
    use_comparative, strategy_name = _select_review_strategy(len(unreviewed))

    await emit_progress(
        state,
        "review_start",
        f"Reviewing {len(unreviewed)} hypotheses...",
        PROGRESS_REVIEW_START,
    )

    reviews, llm_calls = await _run_review_strategy(state, unreviewed, use_comparative)

    attached, failed_count = _finalize_reviews(
        unreviewed, reviews, strategy_name, state.get("criteria")
    )

    await emit_progress(
        state,
        "review_complete",
        f"Completed {len(attached)} reviews",
        PROGRESS_REVIEW_COMPLETE,
        reviews_count=len(attached),
    )

    return attached, llm_calls, strategy_name, failed_count


def _log_review_intake(hypotheses: list[Hypothesis], unreviewed: list[Hypothesis]) -> None:
    logger.info("Starting review node")
    logger.info(
        "Reviewing %s unreviewed of %s hypotheses",
        len(unreviewed),
        len(hypotheses),
    )


def _skipped_review_result(hypotheses: list[Hypothesis]) -> dict[str, Any]:
    return {
        "hypotheses": hypotheses,
        "messages": phase_message(
            "review",
            "No unreviewed hypotheses; review skipped",
            strategy="skipped",
        ),
    }


def _finalize_reviews(
    unreviewed: list[Hypothesis],
    reviews: list[HypothesisReview | None],
    strategy_name: str,
    criteria: list[str] | None = None,
) -> tuple[list[HypothesisReview], int]:
    """Failures remain retryable, never zero-score placeholders; criteria
    choose axes, absent criteria retain defaults."""
    reviewed_pairs, failed_count = _split_reviews_by_result(unreviewed, reviews)
    if failed_count:
        logger.warning(
            "%s/%s reviews failed using %s strategy; those hypotheses"
            " stay unreviewed for the next review pass",
            failed_count,
            len(unreviewed),
            strategy_name,
        )
    reviewed_hypotheses = [hypothesis for hypothesis, _ in reviewed_pairs]
    attached_reviews = [review for _, review in reviewed_pairs]
    _attach_reviews_to_hypotheses(reviewed_hypotheses, attached_reviews)
    apply_initial_review_gate(reviewed_hypotheses, attached_reviews, criteria)
    logger.info(
        "Completed %s reviews using %s strategy",
        len(attached_reviews),
        strategy_name,
    )
    return attached_reviews, failed_count


def _review_node_result(
    hypotheses: list[Hypothesis],
    reviews: list[HypothesisReview],
    llm_calls: int,
    strategy_name: str,
    failed_count: int = 0,
) -> dict[str, Any]:
    metrics = create_metrics_update(deltas=MetricDeltas(reviews=len(reviews), llm_calls=llm_calls))
    logger.debug(
        "review node creating metrics delta: reviews=%s, llm_calls=%s",
        len(reviews),
        llm_calls,
    )

    summary = f"Reviewed {len(reviews)} hypotheses ({strategy_name})"
    if failed_count:
        summary += f"; {failed_count} review(s) failed and will be retried"

    return {
        "hypotheses": hypotheses,
        "metrics": metrics,
        "messages": phase_message(
            "review",
            summary,
            strategy=strategy_name,
            review_failures=failed_count,
        ),
    }
