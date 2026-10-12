from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from co_scientist.core.constants import PROGRESS_META_REVIEW_COMPLETE
from co_scientist.domains.research_state.models import phase_message
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.domains.safety.rules import ContentSafetyReview, review_content_safety
from co_scientist.platform.telemetry.progress import emit_progress

logger = logging.getLogger(__name__)

__all__ = ["MONITOR_STAGE", "monitor_research_direction", "review_direction_safety"]


MONITOR_STAGE = "research_direction"

# Screen all prose directions, including recommendations, so an unsafe
# trajectory cannot bypass monitoring by staying outside the summary.
_MONITORED_FIELDS: tuple[str, ...] = (
    "summary",
    "common_strengths",
    "common_weaknesses",
    "emerging_themes",
    "strategic_recommendations",
)


def _field_text(value: Any) -> str:
    """Lax providers may emit dicts despite schemas; stringify and screen
    them rather than silently bypassing safety."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_field_text(item) for item in value)
    return "" if value is None else str(value)


def _direction_text(meta_review: Mapping[str, Any] | None) -> str:
    overview = meta_review or {}
    return " ".join(_field_text(overview.get(field)) for field in _MONITORED_FIELDS).strip()


def review_direction_safety(
    meta_review: Mapping[str, Any] | None,
) -> ContentSafetyReview:
    return review_content_safety(_direction_text(meta_review), "final")


def _halt_record(review: ContentSafetyReview) -> dict[str, Any]:
    return {
        "stage": MONITOR_STAGE,
        "outcome": review.category,
        "reason": review.reason,
        "matches": list(review.matches),
        "policy_version": review.policy_version,
    }


def _halt_update(state: WorkflowState, review: ContentSafetyReview) -> dict[str, Any]:
    """Safety decisions lack a reducer; explicitly carry the full audit trail
    and tolerate checkpoint-restored None."""
    existing: list[dict[str, Any]] = state.get("safety_decisions") or []
    return {
        "safety_blocked": True,
        "safety_decisions": [*existing, _halt_record(review)],
        "messages": phase_message(
            "safety_monitor",
            "Run halted: the research direction reached prohibited content",
            outcome=review.category,
        ),
    }


async def monitor_research_direction(
    state: WorkflowState, meta_review: Mapping[str, Any] | None
) -> dict[str, Any]:
    review = review_direction_safety(meta_review)
    if review.decision != "block":
        return {}

    logger.error(
        "Safety monitor: halting the run; the research direction matches a "
        "prohibited policy rule (matches=%s)",
        list(review.matches),
    )
    await emit_progress(
        state,
        "safety_monitor_halt",
        "Run halted by the safety monitor",
        PROGRESS_META_REVIEW_COMPLETE,
        outcome=review.category,
    )
    return _halt_update(state, review)
