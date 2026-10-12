from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from co_scientist.domains.safety.rules import POLICY_VERSION, REDACTED_PLACEHOLDER


@dataclass
class SafetyDecision:
    stage: str  # "intake" | "final"
    decision: str  # "allow" | "redact" | "hold" | "block"
    reason: str = ""
    matches: list[str] = field(default_factory=list)
    category: str = "allowed"
    policy_version: str = POLICY_VERSION
    risk_domains: list[str] = field(default_factory=list)
    requires_review: bool = False
    assessor: str = "deterministic"

    def to_dict(self) -> dict[str, str | list[str] | bool]:
        return {
            "stage": self.stage,
            "decision": self.decision,
            "reason": self.reason,
            "matches": self.matches,
            "category": self.category,
            "policy_version": self.policy_version,
            "risk_domains": self.risk_domains,
            "requires_review": self.requires_review,
            "assessor": self.assessor,
        }


def redact_matched_spans(text: str, matches: list[str]) -> str:
    """Matched spans are literal disclosures; scrub every case-insensitive
    occurrence rather than only the first match.
    """
    if not text:
        return text
    out = text
    for span in matches:
        if not span:
            continue
        out = re.sub(re.escape(span), REDACTED_PLACEHOLDER, out, flags=re.I)
    return out


def redact_payload_text(value: Any, matches: list[str]) -> Any:
    """Payload and Markdown are independently readable; scrubbing only one
    leaves the original available through API/events.
    """
    if isinstance(value, str):
        return redact_matched_spans(value, matches)
    if isinstance(value, dict):
        return {key: redact_payload_text(item, matches) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload_text(item, matches) for item in value]
    return value


__all__ = [
    "REDACTED_PLACEHOLDER",
    "SafetyDecision",
    "redact_matched_spans",
    "redact_payload_text",
]
