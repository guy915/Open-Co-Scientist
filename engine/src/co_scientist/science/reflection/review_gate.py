from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from typing import Any

from co_scientist.core.constants import (
    _NEUTRAL_SCORE,
    NEEDS_REVISION_SCORE,
    NOT_VIABLE_SCORE,
)
from co_scientist.domains.research_state.models import (
    SCIENTIST_REVIEWER,
    Hypothesis,
    HypothesisReview,
)
from co_scientist.science.review_summary import MATURE_REVIEW_KEYS


class ReviewType(str, enum.Enum):
    INITIAL = "initial"
    FULL = "full"
    DEEP_VERIFICATION = "deep_verification"
    OBSERVATION = "observation"
    SIMULATION = "simulation"
    RECURRENT = "recurrent"
    FINALIST = "finalist"


_PROMPT_BY_TYPE: dict[ReviewType, str] = {
    ReviewType.INITIAL: "review",
    ReviewType.FULL: "full_review",
    ReviewType.DEEP_VERIFICATION: "deep_verification",
    ReviewType.OBSERVATION: "reflection_observations",
    ReviewType.SIMULATION: "simulation_review",
    ReviewType.RECURRENT: "full_review",
    ReviewType.FINALIST: "finalist_review",
}


def prompt_name_for(review_type: ReviewType) -> str:
    return _PROMPT_BY_TYPE[review_type]


# Revision or partial mechanism support is not a discard signal; only not-viable
# mature findings block, matching the initial rubric.
_FATAL_VERDICTS: dict[ReviewType, frozenset[str]] = {
    ReviewType.FULL: frozenset({"rejected"}),
    ReviewType.RECURRENT: frozenset({"rejected"}),
    ReviewType.SIMULATION: frozenset({"breaks_down"}),
}

# Simulation holds judges only a decisive mechanism step, not overall
# correctness, so it cannot replace the standing disposition.
_VERDICT_DISPOSITIONS: dict[tuple[ReviewType, str], str] = {
    (ReviewType.FULL, "sound"): "viable",
    (ReviewType.FULL, "needs_revision"): "needs_revision",
    (ReviewType.RECURRENT, "sound"): "viable",
    (ReviewType.RECURRENT, "needs_revision"): "needs_revision",
}


def _is_fatal_mature_review(review_type: ReviewType, result: dict[str, Any]) -> bool:
    verdict = str(result.get("verdict") or "")
    return verdict in _FATAL_VERDICTS.get(review_type, frozenset())


def mature_disposition(hypothesis: Hypothesis) -> str | None:
    """Fatal findings outrank narrow simulation success; deeper correctness
    verdicts may revise the initial screen."""
    disposition: str | None = None
    for key in MATURE_REVIEW_KEYS:
        result = hypothesis.enrichments.get(key)
        if not isinstance(result, dict):
            continue
        review_type = ReviewType(key)
        if _is_fatal_mature_review(review_type, result):
            return "inaccurate"
        verdict = str(result.get("verdict") or "")
        disposition = _VERDICT_DISPOSITIONS.get((review_type, verdict), disposition)
    return disposition


def apply_mature_review_disposition(
    hypothesis: Hypothesis, review_type: ReviewType, result: dict[str, Any]
) -> None:
    """Re-read all stored findings rather than making one review transition
    terminal."""
    del review_type, result
    disposition = mature_disposition(hypothesis)
    if disposition is not None:
        hypothesis.review_disposition = disposition


# Verification searches with the finalist review's queries before its call.
VERIFICATION_QUERIES_KEY = "verification_queries"


def finalist_review_needed(hypothesis: Hypothesis) -> bool:
    """A finalist is reviewed in depth once; a failed review is recorded as
    unreviewed and not repaid."""
    return ReviewType.FULL.value not in hypothesis.enrichments


def store_mature_review_result(
    hypothesis: Hypothesis,
    review_type: ReviewType,
    result: dict[str, Any],
    iteration: int,
) -> None:
    """One write path keeps durable and node-level disposition reconciliation
    identical."""
    hypothesis.enrichments[review_type.value] = result
    if review_type is ReviewType.RECURRENT:
        hypothesis.enrichments["recurrent_review_iteration"] = iteration
    apply_mature_review_disposition(hypothesis, review_type, result)


# Default gated axes must remain aligned with the review prompt rubric.
_DEFAULT_GATE_AXES: tuple[str, ...] = ("scientific_soundness", "novelty")

# Safety uses the same not-viable rubric band on every run, before quality axes;
# missing scores stay neutral and rework remains publishable.
_SAFETY_AXIS = "safety"

# A criterion may select multiple axes; unmatched criteria add none. The app
# default Idea correctness must still select scientific_soundness.
_CRITERION_AXIS_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("scientific_soundness", ("sound", "rigor", "accura", "correct")),
    ("plausibility", ("plausib",)),
    ("novelty", ("novel", "origin", "innov")),
    ("relevance", ("relevan", "align")),
    ("testability", ("testab", "falsif", "experiment", "feasib", "verif")),
    ("safety", ("safe", "ethic", "risk")),
    ("clarity", ("clear", "clarit", "precis")),
    ("potential_impact", ("impact", "significan", "translat")),
)


def _gate_score(review: HypothesisReview, criterion: str) -> int:
    """json_object providers may omit required scores; missing is neutral,
    not evidence that the idea deserves the rubric floor."""
    value = review.scores.get(criterion)
    return _NEUTRAL_SCORE if value is None else int(value)


def _gate_axes_for_criteria(criteria: list[str] | None) -> tuple[str, ...]:
    axes: list[str] = []
    for criterion in criteria or []:
        text = str(criterion).lower()
        for axis, keywords in _CRITERION_AXIS_KEYWORDS:
            if axis not in axes and any(word in text for word in keywords):
                axes.append(axis)
    return tuple(axes) or _DEFAULT_GATE_AXES


def _fatal_disposition(fatal_axes: Sequence[str]) -> str:
    """Other fatal selected axes reuse inaccurate because tournament and
    drain consume the canonical blocking disposition vocabulary."""
    soundness_fatal = "scientific_soundness" in fatal_axes
    novelty_fatal = "novelty" in fatal_axes
    if soundness_fatal and novelty_fatal:
        return "inaccurate_and_non_novel"
    if soundness_fatal:
        return "inaccurate"
    if novelty_fatal:
        return "non_novel"
    return "inaccurate"


def _disposition_for(review: HypothesisReview, axes: Sequence[str]) -> str:
    if _gate_score(review, _SAFETY_AXIS) <= NOT_VIABLE_SCORE:
        return "unsafe"
    scores = {axis: _gate_score(review, axis) for axis in axes}
    fatal = [axis for axis in axes if scores[axis] <= NOT_VIABLE_SCORE]
    if fatal:
        return _fatal_disposition(fatal)
    if min(scores.values()) <= NEEDS_REVISION_SCORE:
        return "needs_revision"
    return "viable"


# Scientist reviews score their own assessment, not the agent rubric; neutral
# missing-axis defaults would wrongly clear a blocked idea.
_SCORED_AXES: frozenset[str] = frozenset(axis for axis, _ in _CRITERION_AXIS_KEYWORDS)

# Evidence restoration, failed calls and archived duplicates have other owners;
# recomputing them would strand or resurrect the idea.
_FOREIGN_DISPOSITIONS: frozenset[str] = frozenset(
    {"evidence_blocked", "review_failed", "duplicate"}
)

# The mature correctness cascade never assesses safety and cannot clear it.
_UNSAFE_DISPOSITION = "unsafe"


def _deepest_disposition(hypothesis: Hypothesis, base: str) -> str:
    if base == _UNSAFE_DISPOSITION:
        return base
    return mature_disposition(hypothesis) or base


def _latest_gradable_review(
    hypothesis: Hypothesis,
) -> HypothesisReview | None:
    for review in reversed(hypothesis.reviews):
        if review.reviewer == SCIENTIST_REVIEWER:
            continue
        if _SCORED_AXES.intersection(review.scores):
            return review
    return None


def _latest_scientist_review(
    hypothesis: Hypothesis,
) -> HypothesisReview | None:
    for review in reversed(hypothesis.reviews):
        if review.reviewer == SCIENTIST_REVIEWER:
            return review
    return None


def scientist_disposition(hypothesis: Hypothesis) -> str | None:
    """Human verdicts use the same rubric bands as model reviews, not another
    policy."""
    review = _latest_scientist_review(hypothesis)
    if review is None:
        return None
    if review.overall_score <= NOT_VIABLE_SCORE:
        return "inaccurate"
    if review.overall_score <= NEEDS_REVISION_SCORE:
        return "needs_revision"
    return "viable"


def derive_review_disposition(
    hypothesis: Hypothesis, criteria: list[str] | None = None
) -> str | None:
    """Deeper or scientist verdicts may revise quality; correctness reviews
    and endorsements cannot clear safety."""
    review = _latest_gradable_review(hypothesis)
    base = (
        hypothesis.review_disposition
        if review is None
        else _deepest_disposition(
            hypothesis,
            _disposition_for(review, _gate_axes_for_criteria(criteria)),
        )
    )
    # Scientist endorsement outranks quality judgments, but cannot clear unsafe
    # or foreign evidence/archive dispositions.
    scientist = scientist_disposition(hypothesis)
    if scientist is None or base == _UNSAFE_DISPOSITION:
        return base
    return scientist


def refresh_review_dispositions(
    hypotheses: Iterable[Hypothesis], criteria: list[str] | None = None
) -> int:
    """Recompute after the record grows so one early screen cannot
    permanently exclude an idea from ranking, deeper review and evolution."""
    revised = 0
    for hypothesis in hypotheses:
        if hypothesis.review_disposition in _FOREIGN_DISPOSITIONS:
            continue
        disposition = derive_review_disposition(hypothesis, criteria)
        if disposition != hypothesis.review_disposition:
            hypothesis.review_disposition = disposition
            revised += 1
    return revised


def apply_initial_review_gate(
    hypotheses: list[Hypothesis],
    reviews: list[HypothesisReview],
    criteria: list[str] | None = None,
) -> None:
    """Only not-viable blocks; rework ranks but skips deep review. Safety is
    unconditional, unlike chosen quality axes."""
    axes = _gate_axes_for_criteria(criteria)
    for hypothesis, review in zip(hypotheses, reviews, strict=True):
        base = _disposition_for(review, axes)
        hypothesis.review_disposition = _deepest_disposition(hypothesis, base)


_apply_initial_review_gate = apply_initial_review_gate


# Rechecks must use a review type the mature disposition table actually reads.
RECHECK_REVIEW_TYPE = ReviewType.RECURRENT

# Checkpointed issuance prevents restarts from reopening the recheck wave.
RECHECK_MARKER = "review_recheck_issued"

# The ceiling bounds cost even when the pool grows beyond the initial cohort.
MAX_RECHECKS_PER_RUN = 24

# Only this gate owns these quality blocks; an allow-list prevents new foreign
# dispositions becoming silently eligible for recheck.
_RECHECKABLE_DISPOSITIONS: frozenset[str] = frozenset(
    {"inaccurate", "non_novel", "inaccurate_and_non_novel"}
)


def recheck_issued(hypothesis: Hypothesis) -> bool:
    return bool(hypothesis.enrichments.get(RECHECK_MARKER))


def mark_recheck_issued(hypothesis: Hypothesis) -> None:
    """A failed call spent its recheck; success-only marking would reopen it
    each cycle."""
    hypothesis.enrichments[RECHECK_MARKER] = True


def _is_recheckable(hypothesis: Hypothesis) -> bool:
    """A recurrent sound verdict cannot override a fatal mature cascade
    finding, so spending a recheck on that block cannot help."""
    if hypothesis.review_disposition not in _RECHECKABLE_DISPOSITIONS:
        return False
    # Scientist opposition will be restored regardless of the recurrent result;
    # rechecking it only spends a call.
    if scientist_disposition(hypothesis) == "inaccurate":
        return False
    return mature_disposition(hypothesis) is None


def recheck_targets(hypotheses: Iterable[Hypothesis]) -> list[Hypothesis]:
    """Derive bounds from checkpointed records so restart cannot reopen spent
    attempts."""
    issued = 0
    pending: list[Hypothesis] = []
    for hypothesis in hypotheses:
        if recheck_issued(hypothesis):
            issued += 1
        elif _is_recheckable(hypothesis):
            pending.append(hypothesis)
    return pending[: max(MAX_RECHECKS_PER_RUN - issued, 0)]
