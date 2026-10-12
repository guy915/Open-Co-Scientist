from __future__ import annotations

import dataclasses
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from co_scientist.domains.research_state.repository import hypotheses as store
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.hypotheses import HypothesisStateChanges
from co_scientist.domains.research_state.repository.records import NewSafetyDecision
from co_scientist.domains.safety.hypothesis.safety import (
    EscalatedVerdict,
    HeldHypothesis,
    HypothesisSafetyOutcome,
    HypothesisSafetyReview,
    is_blocking_status,
    redact_fields,
    review_hypothesis_safety,
)
from co_scientist.domains.safety.hypothesis_text import redactable_fields, screened_text

logger = logging.getLogger(__name__)

_REDACTING_OUTCOMES = frozenset({HypothesisSafetyOutcome.REDACT, HypothesisSafetyOutcome.DUAL_USE})

# Keep redactions and contextual blocks after their triggers disappear.
# Cleared/uncertain holds re-resolve each pass as text or eligibility changes.
_STICKY_STATUSES = frozenset(o.value for o in _REDACTING_OUTCOMES) | frozenset(
    {
        HypothesisSafetyOutcome.PROHIBITED.value,
        HypothesisSafetyOutcome.ETHICAL_CONCERN.value,
    }
)

# Hide every screened field but the statement, which stays so the hypothesis
# remains rankable.
_REDACTED_FIELDS = redactable_fields("store")


def hypothesis_text(hyp: Mapping[str, Any]) -> str:
    return screened_text(hyp, "store")


def _apply_redaction(
    run_id: str,
    hyp: Mapping[str, Any],
    review: HypothesisSafetyReview,
    *,
    conn: sqlite3.Connection | None,
    db_path: str | None,
) -> None:
    changed = _changed_redacted_fields(hyp)
    if not changed:
        return
    store.redact_hypothesis_fields(str(hyp["id"]), changed, conn=conn, db_path=db_path)
    if isinstance(hyp, dict):
        hyp.update(changed)
    records.add_safety_decision(
        NewSafetyDecision(
            run_id=run_id,
            stage="hypothesis",
            decision="redact",
            reason=f"hypothesis {hyp['id']}: {review.outcome.value} "
            f"({review.reason}); redacted {sorted(changed)}",
            matches=list(review.matches),
        ),
        db_path=db_path,
        conn=conn,
    )
    # A successful per-item redaction is already audited; logging it as warning
    # would crowd genuine incidents out of the bounded window.
    logger.info(
        "Redacted detail fields of hypothesis %s: %s",
        hyp["id"],
        review.outcome.value,
    )


def _changed_redacted_fields(hyp: Mapping[str, Any]) -> dict[str, str]:
    fields = {f: str(hyp.get(f) or "") for f in _REDACTED_FIELDS}
    redacted = redact_fields(fields)
    return {k: v for k, v in redacted.items() if v != fields[k]}


def _hypothesis_decision_row(
    run_id: str,
    hyp_id: Any,
    review: HypothesisSafetyReview,
    decision: str,
) -> NewSafetyDecision:
    """Record the actual contextual outcome, including a cleared hold; a
    hardcoded block would lie in publication and adjudication audit.
    """
    return NewSafetyDecision(
        run_id=run_id,
        stage="hypothesis",
        decision=decision,
        reason=(f"hypothesis {hyp_id}: {review.outcome.value} ({review.reason})"),
        matches=list(review.matches),
    )


def record_hypothesis_block(
    run_id: str,
    hyp_id: Any,
    review: HypothesisSafetyReview,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> None:
    records.add_safety_decision(
        _hypothesis_decision_row(run_id, hyp_id, review, "block"),
        db_path=db_path,
        conn=conn,
    )


@dataclasses.dataclass(frozen=True)
class ScreeningResult:
    blocked_ids: frozenset[str]
    status_by_id: Mapping[str, str]
    escalatable: tuple[HeldHypothesis, ...] = ()

    @property
    def blocked_count(self) -> int:
        return len(self.blocked_ids)

    @property
    def screened_count(self) -> int:
        return len(self.status_by_id)


def screen_hypotheses(
    run_id: str,
    hyps: Sequence[Mapping[str, Any]],
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> ScreeningResult:
    blocked: set[str] = set()
    status_by_id: dict[str, str] = {}
    escalatable: list[HeldHypothesis] = []
    for hyp in hyps:
        screened = _screen_one_hypothesis(run_id, hyp, conn=conn, db_path=db_path)
        if screened is None:
            continue
        hyp_id, status, is_blocked, held = screened
        status_by_id[hyp_id] = status
        if is_blocked:
            blocked.add(hyp_id)
        if held is not None:
            escalatable.append(held)
    return ScreeningResult(
        blocked_ids=frozenset(blocked),
        status_by_id=status_by_id,
        escalatable=tuple(escalatable),
    )


def _held_for_escalation(
    hyp_id: str, text: str, review: HypothesisSafetyReview
) -> HeldHypothesis | None:
    """Only fresh needs-context Tier B uncertainty is eligible; certain Tier
    A outcomes cannot enter the resolution path.
    """
    if review.outcome is not HypothesisSafetyOutcome.UNCERTAIN:
        return None
    if not review.needs_context:
        return None
    return HeldHypothesis(hyp_id=hyp_id, text=text, review=review)


def _screen_one_hypothesis(
    run_id: str,
    hyp: Mapping[str, Any],
    *,
    conn: sqlite3.Connection | None,
    db_path: str | None,
) -> tuple[str, str, bool, HeldHypothesis | None] | None:
    hyp_id = str(hyp.get("id") or "")
    if not hyp_id:
        return None
    prior = str(hyp.get("safety_status") or "")
    if prior in _STICKY_STATUSES:
        return hyp_id, prior, is_blocking_status(prior), None
    text = hypothesis_text(hyp)
    review = review_hypothesis_safety(text)
    store.update_hypothesis_state(
        hyp_id,
        HypothesisStateChanges(safety_status=review.outcome.value),
        db_path=db_path,
        conn=conn,
    )
    if review.outcome in _REDACTING_OUTCOMES:
        _apply_redaction(run_id, hyp, review, conn=conn, db_path=db_path)
    if review.blocks_tournament:
        record_hypothesis_block(run_id, hyp_id, review, conn=conn, db_path=db_path)
        logger.warning(
            "Excluding hypothesis %s from the tournament: %s",
            hyp_id,
            review.outcome.value,
        )
    return (
        hyp_id,
        review.outcome.value,
        review.blocks_tournament,
        _held_for_escalation(hyp_id, text, review),
    )


def persist_escalated_verdicts(
    run_id: str,
    verdicts: Sequence[EscalatedVerdict],
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> int:
    """Provider assessment must finish outside write transactions; unchanged
    held outcomes already have an audit row and must not be recorded
    twice.
    """
    resolved = 0
    for verdict in verdicts:
        if not verdict.raised:
            continue
        _persist_one_resolved_verdict(run_id, verdict, conn, db_path)
        resolved += 1
    return resolved


def _persist_one_resolved_verdict(
    run_id: str,
    verdict: EscalatedVerdict,
    conn: sqlite3.Connection | None,
    db_path: str | None,
) -> None:
    outcome = verdict.review.outcome.value
    blocking = is_blocking_status(outcome)
    store.update_hypothesis_state(
        verdict.hyp_id,
        HypothesisStateChanges(safety_status=outcome),
        db_path=db_path,
        conn=conn,
    )
    records.add_safety_decision(
        _hypothesis_decision_row(
            run_id,
            verdict.hyp_id,
            verdict.review,
            "block" if blocking else "allow",
        ),
        db_path=db_path,
        conn=conn,
    )
    logger.warning(
        "Contextual assessment resolved held hypothesis %s to %s.",
        verdict.hyp_id,
        outcome,
    )
