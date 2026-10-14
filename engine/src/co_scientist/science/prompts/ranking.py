from dataclasses import dataclass
from typing import Any

from co_scientist.science.prompts._common import (
    PromptRunContext,
    PromptSections,
    _format_bullet_section,
    _format_meta_review_context,
    _run_guidance_section,
)
from co_scientist.science.prompts.generation_draft import format_preferences
from co_scientist.science.prompts.loading import _build_prompt, _get_domain_variables

_NO_REVIEW = "No review material available for this hypothesis yet."

_NO_REFLECTION = "No reflection notes available."


@dataclass(frozen=True)
class RankingSide:
    text: str
    review: dict[str, Any] | None = None
    reflection_notes: str | None = None
    deep_verification: dict[str, Any] | None = None
    mature_reviews: dict[str, dict[str, Any]] | None = None


def _format_review_scores(review: dict[str, Any] | None) -> list[str]:
    if not isinstance(review, dict):
        return []

    lines = ["Review scores:\n"]
    for criterion, score in review.get("scores", {}).items():
        lines.append(f"- {criterion}: {score}\n")
    if "overall_score" in review:
        lines.append(f"- Overall Score: {review['overall_score']}\n")
    return lines


def _format_probe_lines(probe: dict[str, Any]) -> list[str]:
    question = probe.get("question", "")
    answer = probe.get("answer", "")
    fundamental = " (fundamental assumption)" if probe.get("assumption_is_fundamental") else ""
    lines = [f"- Q{fundamental}: {question}\n"]
    if answer:
        lines.append(f"  A: {answer}\n")
    return lines


def _format_deep_verification_context(
    probes: list[dict[str, Any]] | None, verdict: str | None, label: str
) -> str:
    if not probes:
        return ""

    verdict_text = verdict or "unknown"
    sections = [f"\nHypothesis {label} Deep Verification (verdict: {verdict_text}):\n"]
    for probe in probes:
        sections.extend(_format_probe_lines(probe))

    return "".join(sections)


_MATURE_REVIEW_SECTION_LABELS: dict[str, str] = {
    "full": "Full review",
    "simulation": "Simulation review",
    "recurrent": "Recurrent review",
}


def _format_mature_review_lines(section: str, review: dict[str, Any]) -> list[str]:
    lines = [f"- {section} verdict: {review.get('verdict', 'unknown')}\n"]
    justification = review.get("justification")
    if justification:
        lines.append(f"  Justification: {justification}\n")
    for assumption in review.get("assumptions_likely_false") or []:
        lines.append(f"  Assumption likely false: {assumption}\n")
    decisive_step = review.get("decisive_step")
    if decisive_step:
        lines.append(f"  Decisive step: {decisive_step}\n")
    for point in review.get("failure_points") or []:
        lines.append(f"  Failure point: {point}\n")
    return lines


def _format_mature_reviews_context(
    mature_reviews: dict[str, dict[str, Any]] | None, label: str
) -> str:
    if not mature_reviews:
        return ""

    sections = [f"\nHypothesis {label} Mature Review Findings:\n"]
    for key, section_label in _MATURE_REVIEW_SECTION_LABELS.items():
        review = mature_reviews.get(key)
        if review:
            sections.extend(_format_mature_review_lines(section_label, review))

    return "".join(sections)


def format_side_review(side: RankingSide, label: str) -> str:
    deep_verification = side.deep_verification or {}
    parts = _format_review_scores(side.review)
    parts.append(
        f"Reflection Notes (observation analysis): {side.reflection_notes or _NO_REFLECTION}\n"
    )
    parts.append(
        _format_deep_verification_context(
            deep_verification.get("probes"),
            deep_verification.get("verdict"),
            label,
        )
    )
    parts.append(_format_mature_reviews_context(side.mature_reviews, label))
    return "".join(parts).strip() or _NO_REVIEW


# Scientist criteria govern verdicts; fixed review axes remain
# schema/persistence dimensions.
_CRITERIA_PREAMBLE = (
    "**Scientist Evaluation Criteria (governing)**\n"
    "The scientist who commissioned this research specified what matters "
    "most for this goal. Treat these criteria as the governing standard "
    "of your verdict: decide the comparison by them first, and where one "
    "overlaps an evaluation aspect or review score axis (for example a "
    "resource criterion with desirability for implementation, or a "
    "specificity criterion with sufficiency of detail), let the "
    "scientist's framing of that axis weigh heaviest:\n"
)

# Use the existing considerations slot for run context rather than changing the
# prompt headings.
_NO_NOTES = "No additional considerations for this comparison.\n"


def _format_ranking_evaluation_criteria(
    criteria: list[str] | None,
) -> str:
    cleaned = [str(item).strip() for item in criteria or [] if str(item).strip()]
    if not cleaned:
        return ""

    sections = ["\n", _CRITERIA_PREAMBLE]
    sections.extend(f"- {item}\n" for item in cleaned)
    return "".join(sections)


# Preferences always render their default; governing criteria remain optional.
def _format_ranking_preferences(preferences: str | None) -> str:
    return f"{format_preferences(preferences)}\n"


def _format_ranking_notes(context: PromptRunContext) -> str:
    domain = _get_domain_variables(context.tool_registry)
    blocks = (
        domain["domain_context"],
        domain["domain_review_guidance"],
        _format_key_areas_guidance(
            context.supervisor_guidance,
            "Key Research Areas to Consider",
            "When comparing hypotheses, prioritize those that better address these key areas.",
        ),
        _format_meta_review_context(context.meta_review),
        _run_guidance_section(context),
    )
    present = [block.strip() for block in blocks if block and block.strip()]
    if not present:
        return _NO_NOTES
    return "\n\n".join(present) + "\n"


def _build_ranking_prompt_variables(
    research_goal: str,
    side_a: RankingSide,
    side_b: RankingSide,
    context: PromptRunContext,
) -> dict[str, Any]:
    return {
        "research_goal": research_goal,
        "hypothesis_a": side_a.text,
        "hypothesis_b": side_b.text,
        # Each side carries its own initial, reflection, deep and mature review
        # evidence.
        "review_1": format_side_review(side_a, "1"),
        "review_2": format_side_review(side_b, "2"),
        # Always supply these slots, even when empty, to avoid MISSING.
        "evaluation_criteria": _format_ranking_evaluation_criteria(context.criteria),
        "preferences": _format_ranking_preferences(context.preferences),
        "notes": _format_ranking_notes(context),
    }


def get_ranking_prompt(
    research_goal: str,
    side_a: RankingSide,
    side_b: RankingSide,
    context: PromptRunContext | None = None,
    *,
    debate: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "ranking_debate" if debate else "ranking_pairwise",
        _build_ranking_prompt_variables(research_goal, side_a, side_b, ctx),
        tool_registry=ctx.tool_registry,
    )


def get_proximity_prompt(
    hypotheses: list[Any], supervisor_guidance: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any] | None]:
    import json

    return _build_prompt(
        "proximity",
        {
            "hypotheses": json.dumps(
                [h["text"] if isinstance(h, dict) else h for h in hypotheses],
                indent=2,
            )
        },
        sections=PromptSections(
            supervisor_guidance=_format_key_areas_guidance(
                supervisor_guidance,
                "Key Research Areas",
                "When assessing similarity, consider whether hypotheses"
                " explore different aspects of these key areas. Hypotheses"
                " that address the same area with similar approaches should"
                " be flagged as duplicates.",
            )
        ),
        include_domain=False,
    )


def _format_key_areas_guidance(
    supervisor_guidance: dict[str, Any] | None, header: str, trailer: str
) -> str:
    if not supervisor_guidance or not isinstance(supervisor_guidance, dict):
        return ""

    goal_analysis = supervisor_guidance.get("research_goal_analysis", {})
    key_areas = goal_analysis.get("key_areas", [])
    if not key_areas:
        return ""

    bullets = _format_bullet_section(header, key_areas)
    return f"## Supervisor Guidance\n{bullets}{trailer}\n"
