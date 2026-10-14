from __future__ import annotations

from typing import Any

# Full and recurrent use the same schema and therefore the same projection.
MATURE_REVIEW_KEYS: tuple[str, ...] = ("full", "simulation", "recurrent")

_MAX_JUSTIFICATION_CHARS = 400
_MAX_STEP_CHARS = 200
_MAX_FAILURE_POINT_CHARS = 160
_MAX_FAILURE_POINTS = 3
_MAX_FALSE_ASSUMPTIONS = 3


def _clip_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _project_full_review(review: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    verdict = str(review.get("verdict") or "")
    if verdict:
        projected["verdict"] = verdict
    justification = _clip_text(review.get("justification"), _MAX_JUSTIFICATION_CHARS)
    if justification:
        projected["justification"] = justification
    false_assumptions = [
        _clip_text(item.get("assumption"), _MAX_FAILURE_POINT_CHARS)
        for item in review.get("assumptions") or []
        if isinstance(item, dict) and item.get("support") == "likely_false"
    ][:_MAX_FALSE_ASSUMPTIONS]
    if false_assumptions:
        projected["assumptions_likely_false"] = false_assumptions
    return projected


def _project_simulation_review(review: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    verdict = str(review.get("verdict") or "")
    if verdict:
        projected["verdict"] = verdict
    decisive_step = _clip_text(review.get("decisive_step"), _MAX_STEP_CHARS)
    if decisive_step:
        projected["decisive_step"] = decisive_step
    failure_points = [
        _clip_text(point, _MAX_FAILURE_POINT_CHARS) for point in review.get("failure_points") or []
    ][:_MAX_FAILURE_POINTS]
    if failure_points:
        projected["failure_points"] = failure_points
    return projected


def mature_review_summary(
    enrichments: dict[str, Any] | None,
) -> dict[str, dict[str, Any]] | None:
    """Ranking reads this quadratically per cycle; cap prose and exclude
    retrieval bookkeeping that belongs to the review provenance."""
    if not enrichments:
        return None
    summary: dict[str, dict[str, Any]] = {}
    for key in MATURE_REVIEW_KEYS:
        review = enrichments.get(key)
        if not isinstance(review, dict):
            continue
        projected = (
            _project_simulation_review(review)
            if key == "simulation"
            else _project_full_review(review)
        )
        if projected:
            summary[key] = projected
    return summary or None
