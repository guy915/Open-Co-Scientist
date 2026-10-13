from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeAlias

from co_scientist.core.async_bridge import propagate_context
from co_scientist.domains.safety.hypothesis_text import redact_value
from co_scientist.domains.safety.rules import (
    POLICY_VERSION,
    REDACTED_PLACEHOLDER,
    SafetyOutcome,
    SafetyReview,
    is_blocking_status,
    review_hypothesis_safety,
)
from co_scientist.platform.llm.scoped_loop import run_in_scoped_loop

logger = logging.getLogger(__name__)

_STAGE = "hypothesis"

_CLEARED_REASON = (
    "matched a sensitive category term, and a contextual assessment read "
    "the surrounding text as descriptive rather than operational"
)


def is_resolvable_hold(review: SafetyReview) -> bool:
    """Only the Tier B resolver sets needs_context; no model output may
    route a certain Tier A verdict through this gate.
    """
    return bool(review.needs_context) and (review.outcome == SafetyOutcome.UNCERTAIN)


async def resolve_hold(
    review: SafetyReview,
    text: str,
    *,
    run_id: str,
    db_path: str | None = None,
) -> SafetyReview:
    """Only successfully parsed affirmative assessments resolve a hold;
    every non-answer or failure leaves it unchanged.
    """
    if not is_resolvable_hold(review):
        return review
    import co_scientist.domains.safety.gate as app_safety

    decision = await app_safety.assess_hold_contextually(run_id, text, _STAGE, db_path=db_path)
    if decision is None:
        return review
    if decision.decision == "block":
        return SafetyReview(
            SafetyOutcome.PROHIBITED,
            decision.reason,
            tuple(decision.matches) or review.matches,
            POLICY_VERSION,
            True,
        )
    if decision.decision == "allow":
        logger.info(
            "Contextual assessment cleared a held hypothesis in run %s (category term %s).",
            run_id,
            ", ".join(review.matches) or "unrecorded",
        )
        return SafetyReview(
            SafetyOutcome.ALLOW,
            _CLEARED_REASON,
            review.matches,
            POLICY_VERSION,
            True,
        )
    return review


# Compatibility names alias the engine's canonical policy types rather than
# create a parallel safety implementation.
HypothesisSafetyOutcome: TypeAlias = SafetyOutcome
HypothesisSafetyReview: TypeAlias = SafetyReview


def redact_fields(fields: dict[str, str]) -> dict[str, str]:
    return {key: redact_value(key, value) or value for key, value in fields.items()}


async def escalate_review(
    review: SafetyReview,
    text: str,
    *,
    run_id: str,
    db_path: str | None = None,
) -> SafetyReview:
    """Tier B asks whether risk exists and may resolve either way; Tier A
    and intake/final hazard floors cannot be lowered.
    """
    if not review.needs_context or review.outcome != SafetyOutcome.UNCERTAIN:
        return review

    return await resolve_hold(review, text, run_id=run_id, db_path=db_path)


@dataclasses.dataclass(frozen=True)
class HeldHypothesis:
    hyp_id: str
    text: str
    review: SafetyReview


@dataclasses.dataclass(frozen=True)
class EscalatedVerdict:
    """The legacy raised flag means resolved in either direction; an
    unchanged persisted hold requires no further write.
    """

    hyp_id: str
    review: SafetyReview
    raised: bool


# Bound concurrent assessments even when a run holds many hypotheses; ordinary
# clean and Tier A results never need this provider work.
_ESCALATION_CONCURRENCY = 8


def escalate_held_hypotheses(
    run_id: str,
    held: Sequence[HeldHypothesis],
    *,
    db_path: str | None = None,
) -> list[EscalatedVerdict]:
    """Run provider work strictly between the drain's write transactions;
    holding SQLite's single writer over network I/O would stall the app.
    """
    if not held:
        return []

    logger.info(
        "Escalating %d held hypothesis verdict(s) for run %s.",
        len(held),
        run_id,
    )

    def _escalate_one(item: HeldHypothesis) -> EscalatedVerdict:
        # Executor threads have no loop; own and close a call-scoped loop
        # without touching the run loop or leaking LiteLLM workers.
        escalated = run_in_scoped_loop(
            escalate_review(item.review, item.text, run_id=run_id, db_path=db_path)
        )
        return EscalatedVerdict(
            hyp_id=item.hyp_id,
            review=escalated,
            raised=escalated.outcome != item.review.outcome,
        )

    with ThreadPoolExecutor(max_workers=min(_ESCALATION_CONCURRENCY, len(held))) as pool:
        futures = [pool.submit(propagate_context(_escalate_one), item) for item in held]
        return [future.result() for future in futures]


__all__ = [
    "POLICY_VERSION",
    "REDACTED_PLACEHOLDER",
    "EscalatedVerdict",
    "HeldHypothesis",
    "HypothesisSafetyOutcome",
    "HypothesisSafetyReview",
    "escalate_held_hypotheses",
    "escalate_review",
    "is_blocking_status",
    "redact_fields",
    "review_hypothesis_safety",
]
