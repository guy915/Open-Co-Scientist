"""Bare danger nouns need context; regex alone cannot clear them.
Operational harm remains blocked, and unresolved context must abstain safely."""

from __future__ import annotations

import dataclasses
import enum
import re

from co_scientist.domains.safety.hypothesis_text import (
    REDACTED_PLACEHOLDER as REDACTED_PLACEHOLDER,
)
from co_scientist.domains.safety.hypothesis_text import (
    redact_value,
)


def _patterns(*sources: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(s, re.IGNORECASE) for s in sources)


# Action patterns remain binding regardless of context; tolerate padded spelling
# and distant objects.
_PROHIBITED_CERTAIN = _patterns(
    r"\bweapon[\s-]?i[sz]e\b",
    r"\bweaponi[sz]ation\b",
    r"\benhance\b.{0,300}\b(transmissibility|lethality|virulence)\b",
    r"\b(construct|build|assemble|smuggle|deploy)\b.{0,40}"
    r"\b(nuclear|radiological)\b.{0,20}\b(device|weapon|bomb)\b",
    r"\b(design|build|construct|assemble)\b.{0,60}"
    r"\b(explosive device|explosive charge|pipe bomb)\b.{0,100}"
    r"\b(kill|casualt|crowded)\w*",
    # Weapon acquisition is Tier A: contextual Tier B resolution must not clear
    # operational requests.
    r"\b(make|making|obtain(ing)?|acquir(e|ing)|procur(e|ing)"
    r"|prepar(e|ing)|source|sourcing|purchase|purchasing)\b.{0,40}"
    r"\b(nerve agent|chemical weapon|bioweapon|biological weapon"
    r"|sarin|vx|tabun|novichok|anthrax|smallpox|ricin)\b",
    # Improvement is benign process language unless applied to a weapon's
    # effectiveness.
    r"\b(improv|increas|maximi[sz]|boost)\w*\b.{0,40}"
    r"\b(yield|potency|lethality|virulence|transmissibility|dispersal)\b"
    r".{0,60}\b(weapon|agent|bioweapon|toxin|pathogen)\b",
    # Weapon synthesis procedures are operational artifacts; Tier B precedence
    # would otherwise defer them.
    r"\b(step[- ]by[- ]step|detailed|complete|full)\b.{0,40}"
    r"\b(synthesis|protocol|procedure|route|recipe)\b.{0,60}"
    r"\b(nerve agent|nerve toxin|chemical weapon|bioweapon"
    r"|biological weapon|sarin|vx|tabun|novichok|ricin)\b",
    r"\b(synthesis|production) (route|protocol|procedure)\b.{0,40}"
    r"\b(nerve agent|nerve toxin|chemical weapon|bioweapon"
    r"|biological weapon|sarin|vx|tabun|novichok|ricin)\b",
)

_ETHICAL_CERTAIN = _patterns(
    r"\bnon[- ]consensual\b",
)

# Category-only danger needs contextual resolution instead of an unconditional
# block.
_PROHIBITED_CONTEXT = _patterns(
    r"\bmass[- ]casualty\b",
    r"\bbio[- ]?weapon\b",
    # Spelled-out weapon categories must be held just like their contracted
    # forms.
    r"\b(biological|chemical|radiological|nuclear) weapons?\b",
    r"\bnerve (agent|toxin)\b",
    r"\bnerve toxin agent\b",
)

_ETHICAL_CONTEXT = _patterns(
    r"\bwithout (informed )?consent\b",
    r"\beugenic",
)

# Operational intent outranks research framing; a hedge cannot excuse
# construction instructions.
_OPERATIONAL_INTENT_VERBS = _patterns(
    r"\bconstruct(ing|ed)?\b",
    r"\bbuild(ing)?\b",
    r"\bassembl(e|ing|ed)\b",
    r"\bdesign(ing|ed)?\b",
    r"\bdevelop(ing|ed)?\b",
    r"\bengineer(ing|ed)?\b",
    r"\bsynthesi[sz](e|ing|ed)\b",
    r"\bproduc(e|ing|ed)\b",
    r"\bmanufactur(e|ing|ed)\b",
    r"\bdeploy(ing|ed)?\b",
    r"\bweaponi[sz](e|ing|ed)\b",
    r"\benhanc(e|ing|ed)\b",
    r"\bcreat(e|ing|ed)\b",
    r"\bdispers(e|ing|ed)\b",
    r"\breleas(e|ing|ed)\b",
    r"\badminister(ing|ed)?\b",
    r"\bsmuggl(e|ing|ed)\b",
)

# Defensive markers only change a held reason; ambiguous words cannot clear
# danger matches.
_BENIGN_CONTEXT_MARKERS = _patterns(
    r"\breview(ing|s|ed)?\b",
    r"\banaly(?:z|s)(?:e|ing|ed|is)\b",
    r"\bassess(?:ing|ed|ment)?\b",
    r"\bevaluat(?:e|ing|ed|ion)\b",
    r"\bstud(?:y|ying|ied)\b",
    r"\binvestigat(?:e|ing|ed|ion)\b",
    r"\bexamin(?:e|ing|ed|ation)\b",
    r"\bcharacteriz(?:e|ing|ed)\b",
    r"\bhistor(?:y|ical)\b",
    r"\bdeclassified\b",
    r"\bcomplian(?:t|ce)\b",
    r"\bdiagnostic\b",
    r"\btriage\b",
    r"\bdisaster\b",
    r"\bverification\b",
)

_DUAL_USE = _patterns(
    r"\bdual[- ]use\b",
    r"\bselect agent\b",
    r"\bgain[- ]of[- ]function\b",
)

_REDACT = _patterns(
    r"\bsynthesis (route|protocol)\b",
    r"\bstep[- ]by[- ]step\b.{0,30}\b(synthesis|protocol)\b",
)

_OBFUSCATION = _patterns(
    r"\b(for research purposes only|purely hypothetical(ly)?)\b",
    r"\byou know what i mean\b",
)
# Obfuscation requires a sensitive hint plus hedging, never a bare-keyword
# block.
_SENSITIVE_HINT = _patterns(
    r"\b(pathogen|toxin|virus|agent|synthesis|anthrax|smallpox|ebola"
    r"|marburg|nerve agent|chemical weapon|bioweapon|nuclear|radiological"
    r"|radioactive|explosive|bomb)\b",
)

_CONTENT_PROHIBITED = _patterns(
    r"\b(synthes(?:is|e|ize)|engineer|weaponize)\b.{0,40}"
    r"\b(nerve agent|sarin|vx|tabun|novichok)\b",
    r"\b(enhance|engineer|weaponize)\b.{0,40}"
    r"\b(smallpox|anthrax|ebola|marburg)\b.{0,40}"
    r"\b(transmiss|lethal|virulen)",
    r"\b(build|construct|assemble)\b.{0,40}\b(nuclear|radiological)\b"
    r".{0,20}\b(weapon|bomb|device)\b",
    r"\bgain[- ]of[- ]function\b.{0,40}\b(human-to-human|airborne)\b",
    r"\b(produce|manufacture)\b.{0,40}\b(fentanyl|methamphetamine)\b"
    r".{0,20}\b(scale|kilogram)\b",
)
_CONTENT_DUAL_USE = _patterns(
    r"\b(pathogen|toxin|virus|bacterium)\b.{0,30}"
    r"\b(transmiss|lethal|host range)\b",
    r"\b(cbrn|chem-bio|bio-?weapon)\b",
    r"\b(dual[- ]use|select agent)\b",
)


POLICY_VERSION = "coscientist-safety-v5"


class SafetyOutcome(str, enum.Enum):
    PROHIBITED = "prohibited"
    DUAL_USE = "dual_use"
    ETHICAL_CONCERN = "ethical_concern"
    REDACT = "redact"
    UNCERTAIN = "uncertain"
    ALLOW = "allow"


BLOCKING_OUTCOMES = frozenset(
    {
        SafetyOutcome.PROHIBITED,
        SafetyOutcome.ETHICAL_CONCERN,
        SafetyOutcome.UNCERTAIN,
    }
)


def is_blocking_status(status: str | None) -> bool:
    if not status:
        return False
    try:
        return SafetyOutcome(status) in BLOCKING_OUTCOMES
    except ValueError:
        return False


@dataclasses.dataclass(frozen=True)
class SafetyReview:
    """Contextual assessment may clear or block Tier B holds; Tier A verdicts
    remain binding.
    """

    outcome: SafetyOutcome
    reason: str
    matches: tuple[str, ...]
    policy_version: str
    needs_context: bool = False

    @property
    def blocks_tournament(self) -> bool:
        return self.outcome in BLOCKING_OUTCOMES

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "matches": list(self.matches),
            "policy_version": self.policy_version,
            "needs_context": self.needs_context,
        }


@dataclasses.dataclass(frozen=True)
class ContentSafetyReview:
    decision: str
    category: str
    reason: str
    matches: tuple[str, ...]
    risk_domains: tuple[str, ...]
    requires_review: bool
    policy_version: str = POLICY_VERSION


def _all_matches(text: str, patterns: tuple[re.Pattern[str], ...]) -> tuple[str, ...]:
    return tuple(
        match.group(0) for pattern in patterns if (match := pattern.search(text or "")) is not None
    )


def _first_match(text: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None


def _resolve_context_hit(text: str, hit: str, outcome: SafetyOutcome, reason: str) -> SafetyReview:
    """A finite verb list cannot prove benign intent; meta-research markers
    change the held reason only.
    """
    if _first_match(text, _OPERATIONAL_INTENT_VERBS):
        return SafetyReview(outcome, reason, (hit,), POLICY_VERSION, True)
    if _first_match(text, _BENIGN_CONTEXT_MARKERS):
        return SafetyReview(
            SafetyOutcome.UNCERTAIN,
            (
                "matches a sensitive category term in an apparently "
                "descriptive/meta-research context; held for manual "
                "review, not cleared"
            ),
            (hit,),
            POLICY_VERSION,
            True,
        )
    return SafetyReview(
        SafetyOutcome.UNCERTAIN,
        (
            "matches a sensitive category term with no corroborating "
            "signal either way; held for manual review"
        ),
        (hit,),
        POLICY_VERSION,
        True,
    )


@dataclasses.dataclass(frozen=True)
class SafetyRule:
    patterns: tuple[re.Pattern[str], ...]
    stages: frozenset[str]
    outcome: SafetyOutcome
    reason: str
    context: bool = False
    sensitive_hint: bool = False
    content: bool = False


_ALL_STAGES = frozenset({"hypothesis", "intake", "final"})
_CONTENT_STAGES = frozenset({"intake", "final"})

# Order breaks ties: the content prohibition remains binding before contextual
# hypotheses, and whole documents never apply unbounded hedge detection.
SAFETY_RULES = (
    SafetyRule(
        _CONTENT_PROHIBITED,
        _CONTENT_STAGES,
        SafetyOutcome.PROHIBITED,
        "Content matches a prohibited weaponization or mass-casualty policy rule.",
        content=True,
    ),
    SafetyRule(
        _PROHIBITED_CERTAIN,
        _ALL_STAGES,
        SafetyOutcome.PROHIBITED,
        "matches a prohibited weaponization/mass-casualty pattern",
    ),
    SafetyRule(
        _ETHICAL_CERTAIN,
        _ALL_STAGES,
        SafetyOutcome.ETHICAL_CONCERN,
        "raises an ethical-norms concern",
    ),
    SafetyRule(
        _PROHIBITED_CONTEXT,
        _ALL_STAGES,
        SafetyOutcome.PROHIBITED,
        "matches a prohibited weaponization/mass-casualty pattern",
        context=True,
    ),
    SafetyRule(
        _ETHICAL_CONTEXT,
        _ALL_STAGES,
        SafetyOutcome.ETHICAL_CONCERN,
        "raises an ethical-norms concern",
        context=True,
    ),
    SafetyRule(
        _OBFUSCATION,
        frozenset({"hypothesis", "intake"}),
        SafetyOutcome.UNCERTAIN,
        "obfuscated intent around sensitive content; manual review",
        sensitive_hint=True,
    ),
    SafetyRule(
        _CONTENT_DUAL_USE,
        frozenset({"final"}),
        SafetyOutcome.REDACT,
        "Dual-use content requires redaction and explicit oversight.",
        content=True,
    ),
    SafetyRule(
        _DUAL_USE,
        frozenset({"hypothesis"}),
        SafetyOutcome.DUAL_USE,
        "legitimate but dual-use; allow with redaction/annotation",
    ),
    SafetyRule(
        _REDACT,
        frozenset({"hypothesis"}),
        SafetyOutcome.REDACT,
        "contains sensitive operational detail to redact",
    ),
)


def _rule_review(text: str, rule: SafetyRule) -> SafetyReview | None:
    matches = _all_matches(text, rule.patterns) if rule.content else ()
    if not matches:
        hit = _first_match(text, rule.patterns)
        if hit is None:
            return None
        matches = (hit,)
    if rule.sensitive_hint and _first_match(text, _SENSITIVE_HINT) is None:
        return None
    if rule.context:
        return _resolve_context_hit(text, matches[0], rule.outcome, rule.reason)
    return SafetyReview(rule.outcome, rule.reason, matches, POLICY_VERSION)


def _policy_matches(
    text: str, stage: str, *, detect_obfuscation: bool = True
) -> list[tuple[SafetyRule, SafetyReview]]:
    return [
        (rule, review)
        for rule in SAFETY_RULES
        if stage in rule.stages and (detect_obfuscation or not rule.sensitive_hint)
        if (review := _rule_review(text or "", rule)) is not None
    ]


def review_hypothesis_safety(text: str, *, detect_obfuscation: bool = True) -> SafetyReview:
    matches = _policy_matches(text, "hypothesis", detect_obfuscation=detect_obfuscation)
    if matches:
        return matches[0][1]
    return SafetyReview(SafetyOutcome.ALLOW, "no safety concern detected", (), POLICY_VERSION)


_CONTENT_OUTCOMES = {
    SafetyOutcome.PROHIBITED: ("block", "prohibited", "cbrn_weaponization", 3),
    SafetyOutcome.ETHICAL_CONCERN: ("block", "ethical_concern", "research_ethics", 3),
    SafetyOutcome.UNCERTAIN: ("hold", "uncertain", "obfuscated_intent", 2),
    SafetyOutcome.REDACT: ("redact", "redacted", "dual_use", 1),
}


def review_content_safety(text: str, stage: str) -> ContentSafetyReview:
    if stage not in _CONTENT_STAGES:
        raise ValueError("stage must be 'intake' or 'final'")
    matches = _policy_matches(text, stage)
    if not matches:
        return ContentSafetyReview("allow", "allowed", "", (), (), False)
    rule, review = max(matches, key=lambda item: _CONTENT_OUTCOMES[item[1].outcome][3])
    decision, category, risk, _ = _CONTENT_OUTCOMES[review.outcome]
    return ContentSafetyReview(
        decision,
        category,
        review.reason if rule.content else f"Content {review.reason}.",
        review.matches,
        (risk,),
        decision == "hold" or (rule.content and decision == "redact"),
    )


def redact_hypothesis_fields(
    text: str | None,
    explanation: str | None,
    experiment: str | None,
    literature_grounding: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Every free-text field but the statement is hidden, so the hypothesis
    stays rankable while no screened detail survives a redact verdict.
    """
    return (
        text,
        redact_value("explanation", explanation),
        redact_value("experiment", experiment),
        redact_value("literature_grounding", literature_grounding),
    )
