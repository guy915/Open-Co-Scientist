from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import co_scientist.platform.llm.process_mode as process_mode
from co_scientist.core.config import settings
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.safety.rules import (
    POLICY_VERSION,
    review_content_safety,
)
from co_scientist.domains.safety.semantic import (
    run_semantic_safety_model,
    semantic_credential_missing_decision,
    semantic_safety_error_decision,
)
from co_scientist.domains.safety.types import SafetyDecision, redact_matched_spans
from co_scientist.domains.safety.types import redact_payload_text as redact_payload_text
from co_scientist.platform.db import runs

logger = logging.getLogger(__name__)


def _decision_from_review(stage: str, review: Any) -> SafetyDecision:
    return SafetyDecision(
        stage=stage,
        decision=review.decision,
        reason=review.reason,
        matches=list(review.matches),
        category=review.category,
        risk_domains=list(review.risk_domains),
        requires_review=review.requires_review,
        policy_version=review.policy_version,
    )


_DECISION_SEVERITY = {"allow": 0, "redact": 1, "hold": 2, "block": 3}


def _severity(decision: SafetyDecision) -> int:
    return _DECISION_SEVERITY.get(decision.decision, 0)


def _more_severe(first: SafetyDecision, second: SafetyDecision) -> SafetyDecision:
    return second if _severity(second) > _severity(first) else first


def screen_intake(goal: str) -> SafetyDecision:
    return _decision_from_review("intake", review_content_safety(goal or "", "intake"))


def screen_final(report_markdown: str) -> SafetyDecision:
    return _decision_from_review("final", review_content_safety(report_markdown or "", "final"))


async def screen_contextual(
    text: str,
    stage: str,
    *,
    deterministic: SafetyDecision | None = None,
) -> SafetyDecision:
    """Rules are hard floors the model cannot lower; deliberate offline mode
    skips assessment, while a missing configured credential refuses.
    """
    baseline = deterministic or (screen_intake(text) if stage == "intake" else screen_final(text))
    if baseline.decision == "block":
        return baseline
    model = settings.semantic_safety_model or settings.supervisor_model_name or settings.model_name
    assert model is not None
    if not settings.semantic_safety_enabled or process_mode.offline_mode():
        return baseline
    if not process_mode.credential_available(model):
        return semantic_credential_missing_decision(stage, model, baseline)
    try:
        assessment = await run_semantic_safety_model(text, stage, model)
    except Exception as exc:  # Provider failure must not silently clear risk.
        return semantic_safety_error_decision(stage, model, baseline, exc)
    # Deterministic content floors cannot be cleared by a permissive model;
    # contextual assessment may only raise their severity.
    return _more_severe(assessment, baseline)


def _should_escalate_to_semantic(run_id: str, stage: str, *, db_path: str | None) -> bool:
    """Historical eligibility follows the persisted run backend, not current
    process mode.
    """
    approved = records.safety_stage_is_approved(run_id, stage, POLICY_VERSION, db_path=db_path)
    offline = runs.run_offline_backed(run_id, db_path=db_path)
    return not offline and not approved


async def assess_hold_contextually(
    run_id: str,
    text: str,
    stage: str,
    *,
    db_path: str | None = None,
) -> SafetyDecision | None:
    """Tier B needs a verdict distinct from absence: disabled, offline,
    missing credentials or provider failure must never become an allow.
    """
    if not _should_escalate_to_semantic(run_id, stage, db_path=db_path):
        return None
    model = settings.semantic_safety_model or settings.supervisor_model_name or settings.model_name
    assert model is not None
    if not settings.semantic_safety_enabled or process_mode.offline_mode():
        return None
    if not process_mode.credential_available(model):
        return None
    try:
        return await run_semantic_safety_model(text, stage, model)
    except Exception as exc:  # An outage must never read as a clean pass.
        logger.warning("Contextual hold assessment failed: %s", exc)
        return None


@dataclass(frozen=True)
class ScreenSubject:
    stage: str
    text: str
    deterministic: SafetyDecision


async def screen_with_escalation(
    run_id: str,
    subject: ScreenSubject,
    *,
    provider: str,
    db_path: str | None = None,
) -> SafetyDecision:
    """Already human-approved stages and offline runs retain their
    deterministic verdict; historical backend comes from the run row.
    """
    if _should_escalate_to_semantic(run_id, subject.stage, db_path=db_path):
        return ensure_redactable(
            await screen_contextual(
                subject.text,
                subject.stage,
                deterministic=subject.deterministic,
            )
        )
    return ensure_redactable(subject.deterministic)


def ensure_redactable(decision: SafetyDecision) -> SafetyDecision:
    """A redact verdict without matched spans cannot remove text; hold it
    rather than publish untouched content under a redaction label.
    """
    if decision.decision != "redact" or decision.matches:
        return decision
    return SafetyDecision(
        stage=decision.stage,
        decision="hold",
        reason=(
            "Content was marked for redaction but no removable span was "
            "identified; human review required."
        ),
        category=decision.category,
        policy_version=decision.policy_version,
        risk_domains=list(decision.risk_domains) or ["unredactable"],
        requires_review=True,
        assessor=decision.assessor,
    )


__all__ = [
    "POLICY_VERSION",
    "SafetyDecision",
    "ScreenSubject",
    "ensure_redactable",
    "redact_matched_spans",
    "redact_payload_text",
    "screen_contextual",
    "screen_final",
    "screen_intake",
    "screen_with_escalation",
]
