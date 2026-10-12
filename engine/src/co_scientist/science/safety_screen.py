from __future__ import annotations

import logging
import time
from typing import Any, NamedTuple

from co_scientist.core.constants import (
    PROGRESS_SAFETY_SCREEN_COMPLETE,
    PROGRESS_SAFETY_SCREEN_START,
)
from co_scientist.domains.research_state.hypothesis_fields import HYPOTHESIS_FIELDS
from co_scientist.domains.research_state.models import (
    Hypothesis,
    create_metrics_update,
    phase_message,
)
from co_scientist.domains.research_state.state import ReplaceHypotheses, WorkflowState
from co_scientist.domains.safety.hypothesis_text import screened_text
from co_scientist.domains.safety.rules import (
    SafetyOutcome,
    redact_hypothesis_fields,
    review_hypothesis_safety,
)
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.prompts._common import _format_meta_review_context

logger = logging.getLogger(__name__)

__all__ = ["safety_screen_node"]


def _screen_text(h: Hypothesis) -> str:
    fields = {field.engine: getattr(h, field.engine) for field in HYPOTHESIS_FIELDS}
    return screened_text(fields, "engine", separator=" ")


def _build_safety_decision(
    review: Any, h: Hypothesis, meta_review_context: str = ""
) -> dict[str, Any] | None:
    """Omit absent critique to preserve first-cycle audit shapes; it informs
    adjudication, never the screening outcome."""
    if review.outcome == SafetyOutcome.UNCERTAIN or review.blocks_tournament:
        decision: dict[str, Any] = {
            "hypothesis_id": h.id,
            "text_prefix": h.text[:120],
            **review.to_dict(),
        }
        if meta_review_context:
            decision["meta_review_context"] = meta_review_context
        return decision
    return None


def _redact_if_needed(h: Hypothesis, review: Any) -> None:
    if review.outcome in (SafetyOutcome.DUAL_USE, SafetyOutcome.REDACT):
        _, h.explanation, h.experiment, h.literature_grounding = redact_hypothesis_fields(
            h.text, h.explanation, h.experiment, h.literature_grounding
        )


def _screen_one_hypothesis(
    h: Hypothesis, meta_review_context: str = ""
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    if h.safety_status is not None:
        return True, None, None

    review = review_hypothesis_safety(_screen_text(h))
    h.safety_status = review.outcome.value
    decision = _build_safety_decision(review, h, meta_review_context)

    if review.outcome == SafetyOutcome.UNCERTAIN:
        logger.warning(
            "Safety screen: UNCERTAIN hypothesis held for review: %s... (id=%s)",
            h.text[:80],
            h.id,
        )
        return False, decision, h.to_dict()

    if review.blocks_tournament:
        logger.warning(
            "Safety screen: blocked hypothesis (%s): %s... (id=%s)",
            review.outcome.value,
            h.text[:80],
            h.id,
        )
        return False, decision, None

    _redact_if_needed(h, review)
    return True, decision, None


class _ScreenOutcome(NamedTuple):
    safe: list[Hypothesis]
    decisions: list[dict[str, Any]]
    held: list[dict[str, Any]]


def _screen_hypothesis_pool(
    hypotheses: list[Hypothesis], meta_review_context: str = ""
) -> _ScreenOutcome:
    safe: list[Hypothesis] = []
    new_decisions: list[dict[str, Any]] = []
    new_held: list[dict[str, Any]] = []

    for h in hypotheses:
        is_safe, decision, held = _screen_one_hypothesis(h, meta_review_context)
        if decision is not None:
            new_decisions.append(decision)
        if held is not None:
            new_held.append(held)
        if is_safe:
            safe.append(h)

    return _ScreenOutcome(safe, new_decisions, new_held)


def _log_screen_summary(
    hypotheses: list[Hypothesis],
    safe: list[Hypothesis],
    blocked_count: int,
    held_count: int,
) -> None:
    if blocked_count:
        logger.info(
            "Safety screen: %d/%d hypotheses blocked (%d held for review)",
            blocked_count,
            len(hypotheses),
            held_count,
        )

    if not safe and hypotheses:
        logger.warning(
            "Safety screen: ALL %d hypotheses blocked; pool is now empty",
            len(hypotheses),
        )


def _build_screen_result(
    hypotheses: list[Hypothesis],
    outcome: _ScreenOutcome,
    state: WorkflowState,
    elapsed: float,
) -> dict[str, Any]:
    safe = outcome.safe
    blocked_count = len(hypotheses) - len(safe)
    held_count = len(outcome.held)
    # Checkpoint restore may carry explicit None; a get default alone does not
    # cover it.
    existing_decisions: list[dict[str, Any]] = state.get("safety_decisions") or []
    existing_held: list[dict[str, Any]] = state.get("held_for_review") or []
    return {
        "hypotheses": ReplaceHypotheses(safe),
        "safety_decisions": existing_decisions + outcome.decisions,
        "held_for_review": existing_held + outcome.held,
        "messages": phase_message(
            "safety_screen",
            f"Screened {len(hypotheses)} hypotheses: "
            f"{len(safe)} safe, {blocked_count} blocked"
            f" ({held_count} held for review)",
        ),
        "metrics": create_metrics_update(phase_times={"safety_screen": elapsed}),
    }


async def safety_screen_node(
    state: WorkflowState,
) -> dict[str, Any]:
    start = time.time()
    hypotheses: list[Hypothesis] = state.get("hypotheses", [])
    await emit_progress(
        state,
        "safety_screen",
        "Screening hypotheses for safety",
        PROGRESS_SAFETY_SCREEN_START,
        hypotheses_count=len(hypotheses),
    )

    # Critique informs human adjudication only; it cannot change safety outcomes.
    outcome = _screen_hypothesis_pool(
        hypotheses, _format_meta_review_context(state.get("meta_review"))
    )
    safe = outcome.safe
    blocked_count = len(hypotheses) - len(safe)
    held_count = len(outcome.held)
    _log_screen_summary(hypotheses, safe, blocked_count, held_count)
    elapsed = time.time() - start

    await emit_progress(
        state,
        "safety_screen_complete",
        f"Safety screening complete ({blocked_count} blocked)",
        PROGRESS_SAFETY_SCREEN_COMPLETE,
        blocked_count=blocked_count,
        held_count=held_count,
    )

    return _build_screen_result(hypotheses, outcome, state, elapsed)
