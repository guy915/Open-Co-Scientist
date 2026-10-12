from __future__ import annotations

import logging
from typing import Any

from co_scientist.core.json_schema import obj
from co_scientist.core.prompt_layout import render_cacheable_prompt
from co_scientist.domains.safety.types import SafetyDecision
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
    coerce_json_list,
)

logger = logging.getLogger(__name__)

__all__ = [
    "run_semantic_safety_model",
    "semantic_credential_missing_decision",
    "semantic_safety_error_decision",
]


def _semantic_prompt(text: str, stage: str) -> str:
    instructions = (
        "Classify the scientific content under a research safety policy. "
        "Return JSON only with category, reason, risk_domains, "
        "operational_detail, offensive_score, "
        "is_personal_medical_recommendation, and "
        "is_personal_finance_recommendation. category must be one of "
        "prohibited, ethical_concern, uncertain, redacted, allowed. Treat "
        "requests that meaningfully enable weaponization, harmful pathogen "
        "enhancement, mass-casualty capability, non-consensual human "
        "experimentation, or evasion as prohibited or ethical_concern. "
        "Legitimate dual-use work with actionable harmful operational detail "
        "is redacted. Ambiguous risk is uncertain. Do not reproduce or "
        "elaborate unsafe detail.\n"
        "offensive_score is 1-5, where 5 is content a colleague would report "
        "as demeaning, harassing, or discriminatory toward a group of people; "
        "clinical or frank language about a disease, a population, or a body "
        "is 1.\n"
        "The two recommendation flags are true only when the content asks "
        "for, or gives, a course of action for one named person's own health "
        "or money -- their diagnosis, their treatment, their portfolio. "
        "Research about a disease, a drug, a market or a cohort is false, "
        "including when the person writing has the condition themselves."
    )
    return render_cacheable_prompt(
        instructions,
        "",
        f"Stage: {stage}\nContent:\n{text[:16000]}",
        "Classify this content under the policy above.",
    )


# json_object backfills enum omissions with its first value; uncertainty must
# remain the default rather than accidental allowance or prohibition.
_SEMANTIC_CATEGORY_SCHEMA = {
    "type": "string",
    "enum": [
        "uncertain",
        "prohibited",
        "ethical_concern",
        "redacted",
        "allowed",
    ],
}

# Accept bare risk-domain strings before local validation so the readable-list
# coercion can recover unwrapped single values.
_RISK_DOMAINS_SCHEMA = {
    "oneOf": [
        {"type": "array", "items": {"type": "string"}},
        {"type": "string"},
    ]
}

# The prompt's operational_detail informs model reasoning only; the closed
# response schema intentionally omits that unused field.
_SEMANTIC_DECISION_SCHEMA = obj(
    {
        "category": _SEMANTIC_CATEGORY_SCHEMA,
        "reason": {"type": "string"},
        "risk_domains": _RISK_DOMAINS_SCHEMA,
        "offensive_score": {"type": "number"},
        "is_personal_medical_recommendation": {"type": "boolean"},
        "is_personal_finance_recommendation": {"type": "boolean"},
    },
    optional=(
        "risk_domains",
        "is_personal_medical_recommendation",
        "is_personal_finance_recommendation",
    ),
)

_SEMANTIC_CATEGORY_TO_DECISION = {
    "prohibited": "block",
    "ethical_concern": "block",
    "uncertain": "hold",
    "redacted": "redact",
    "allowed": "allow",
}

# Structured misuse/tone flags can hold an allowed result for human review; they
# cannot lower withheld verdicts or create a hard-hazard block.
_OFFENSIVE_HOLD_SCORE = 4

_PERSONAL_RECOMMENDATION_FLAGS = {
    "is_personal_medical_recommendation": "personal_medical_recommendation",
    "is_personal_finance_recommendation": "personal_finance_recommendation",
}


def _offensive_score(parsed: dict[str, Any]) -> float:
    try:
        return float(parsed.get("offensive_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _structured_flag_domains(parsed: dict[str, Any]) -> list[str]:
    domains = [
        domain for key, domain in _PERSONAL_RECOMMENDATION_FLAGS.items() if parsed.get(key) is True
    ]
    if _offensive_score(parsed) >= _OFFENSIVE_HOLD_SCORE:
        domains.append("offensive_content")
    return domains


_FLAG_HOLD_REASONS = {
    "personal_medical_recommendation": (
        "reads as a personal medical recommendation rather than a research goal"
    ),
    "personal_finance_recommendation": (
        "reads as a personal financial recommendation rather than a research goal"
    ),
    "offensive_content": "was assessed as offensive toward a group of people",
}


def _flag_hold_reason(domains: list[str]) -> str:
    causes = " and it ".join(
        _FLAG_HOLD_REASONS[domain] for domain in domains if domain in _FLAG_HOLD_REASONS
    )
    return f"Content {causes}; human review required."


async def _call_semantic_safety_model(text: str, stage: str, model: str) -> dict[str, Any]:
    """Use shared structured parsing and physical-call metering; json_object
    gateways may fence or reshape otherwise valid JSON.
    """
    from co_scientist.core import byok_scope

    # Scoped BYOK selects its own model and credential rather than using the
    # deployment's account.
    resolved_model, api_key = byok_scope.byok_model_and_key(model)
    spec = CompletionSpec(
        role="safety",
        model_name=resolved_model,
        max_tokens=1_000,
        temperature=0,
        json_schema=_SEMANTIC_DECISION_SCHEMA,
        api_key=api_key,
    )
    # Re-screen rather than cache old safety verdicts; two physical attempts
    # bound bootstrap's first lease without a five-attempt timeout wait.
    result: dict[str, Any] = await call_llm_json(
        _semantic_prompt(text, stage),
        spec,
        max_attempts=2,
        options=LLMCallOptions(prompt_name="safety_screen"),
    )
    return result


def _merge_risk_domains(parsed: dict[str, Any], flagged: list[str]) -> list[str]:
    """Deduplicate free-text and structured domains so one risk does not
    appear as independent findings.
    """
    merged: list[str] = coerce_json_list(
        parsed.get("risk_domains"),
        element="str",
        site="safety_semantic.risk_domains",
    )
    merged.extend(domain for domain in flagged if domain not in merged)
    return merged


def _build_semantic_decision(stage: str, model: str, parsed: dict[str, Any]) -> SafetyDecision:
    category = str(parsed.get("category") or "uncertain")
    if category not in _SEMANTIC_CATEGORY_TO_DECISION:
        category = "uncertain"
    decision = _SEMANTIC_CATEGORY_TO_DECISION[category]
    reason = str(parsed.get("reason") or "Contextual safety assessment.")
    flagged = _structured_flag_domains(parsed)
    # Flags may hold only a clean allow; preserve the model's already-withheld
    # verdict and reason.
    if flagged and decision == "allow":
        decision = "hold"
        category = "uncertain"
        reason = _flag_hold_reason(flagged)
    return SafetyDecision(
        stage=stage,
        decision=decision,
        reason=reason,
        category=category,
        risk_domains=_merge_risk_domains(parsed, flagged),
        requires_review=decision in {"hold", "redact"},
        assessor=f"semantic:{model}",
    )


async def run_semantic_safety_model(text: str, stage: str, model: str) -> SafetyDecision:
    parsed = await _call_semantic_safety_model(text, stage, model)
    return _build_semantic_decision(stage, model, parsed)


def _assessment_unavailable_decision(
    stage: str, baseline: SafetyDecision, assessor: str
) -> SafetyDecision:
    """Configured assessment failure cannot count as clean: keep an existing
    redaction, otherwise hold for review.
    """
    if baseline.decision == "redact":
        return baseline
    return SafetyDecision(
        stage=stage,
        decision="hold",
        reason=("Contextual safety assessment was unavailable; human review required."),
        category="uncertain",
        risk_domains=["assessment_unavailable"],
        requires_review=True,
        assessor=assessor,
    )


def semantic_safety_error_decision(
    stage: str, model: str, baseline: SafetyDecision, exc: Exception
) -> SafetyDecision:
    logger.warning("Contextual safety assessment failed: %s", exc)
    return _assessment_unavailable_decision(stage, baseline, f"semantic:{model}:error")


def semantic_credential_missing_decision(
    stage: str, model: str, baseline: SafetyDecision
) -> SafetyDecision:
    """A missing configured credential is a deployment fault, not evidence
    of safe content; refuse rather than return the baseline.
    """
    logger.warning(
        "Contextual safety assessment is enabled but model %s has no "
        "reachable provider credential; withholding %s stage for human "
        "review instead of screening on the deterministic rules alone.",
        model,
        stage,
    )
    return _assessment_unavailable_decision(stage, baseline, f"semantic:{model}:no_credential")
