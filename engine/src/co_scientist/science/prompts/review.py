from typing import Any

from co_scientist.science.prompts._common import (
    PromptRunContext,
    PromptSections,
    _format_meta_review_context,
    _guidance_items,
    _run_guidance_section,
)
from co_scientist.science.prompts.loading import _build_prompt
from co_scientist.science.schemas.planning import (
    CRITICAL_CRITERIA_MAX_COUNT,
    CRITICAL_CRITERIA_MAX_QUESTIONS,
)


def _review_sections(context: PromptRunContext) -> PromptSections:
    """Coverage cues must not bias the sticky initial novelty gate against
    Evolution refinements.
    """
    return PromptSections(
        supervisor_guidance=_format_supervisor_guidance_for_review(context.supervisor_guidance),
        meta_review_context=_format_meta_review_context(
            context.meta_review, include_coverage_sections=False
        ),
        run_guidance=_run_guidance_section(context),
    )


def get_review_prompt(
    research_goal: str,
    hypothesis_text: str,
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "review",
        {"research_goal": research_goal, "hypothesis_text": hypothesis_text},
        sections=_review_sections(ctx),
        tool_registry=ctx.tool_registry,
    )


def get_deep_verification_prompt(
    research_goal: str,
    hypothesis_text: str,
    tool_registry: Any | None = None,
    evidence_context: str = "No retrieved evidence available.",
) -> tuple[str, dict[str, Any] | None]:
    """Probes challenge the hypothesis on its own terms, without supervisor
    guidance.
    """
    return _build_prompt(
        "deep_verification",
        {
            "research_goal": research_goal,
            "hypothesis_text": hypothesis_text,
            "evidence_context": evidence_context,
        },
        tool_registry=tool_registry,
    )


def get_review_batch_prompt(
    research_goal: str,
    hypotheses_list: str,
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "review_batch",
        {"research_goal": research_goal, "hypotheses_list": hypotheses_list},
        sections=_review_sections(ctx),
        tool_registry=ctx.tool_registry,
    )


def _format_critical_criterion_question(question: Any) -> str:
    """Malformed model output and old checkpoints must still render."""
    if isinstance(question, str):
        text = question.strip()
        return f"  - {text}\n" if text else ""
    if not isinstance(question, dict):
        return ""
    name = str(question.get("name") or "").strip()
    text = str(question.get("question") or "").strip()
    if not text:
        return ""
    return f"  - **{name}:** {text}\n" if name else f"  - {text}\n"


def _format_critical_criterion_questions(questions: Any) -> list[str]:
    """json_object mode does not enforce schema maxItems; bound the rendered
    questions defensively.
    """
    lines = []
    items = _guidance_items(questions)[:CRITICAL_CRITERIA_MAX_QUESTIONS]
    for question in items:
        line = _format_critical_criterion_question(question)
        if line:
            lines.append(line)
    return lines


def _format_critical_criterion(criterion: Any) -> list[str]:
    """Accept legacy criterion names; prose descriptions belong in reports, not
    every hypothesis review.
    """
    if isinstance(criterion, str):
        name = criterion.strip()
        return [f"- {name}\n"] if name else []
    if not isinstance(criterion, dict):
        return []
    name = str(criterion.get("name") or "").strip()
    if not name:
        return []
    return [
        f"- **{name}**\n",
        *_format_critical_criterion_questions(criterion.get("questions")),
    ]


def _format_review_phase_guidance(review_phase: dict[str, Any]) -> list[str]:
    if not review_phase:
        return []

    sections = ["## Supervisor Guidance for Review\n"]
    criteria = _guidance_items(review_phase.get("critical_criteria"))
    if criteria:
        sections.append("**Critical Criteria to Emphasize:**\n")
        for criterion in criteria[:CRITICAL_CRITERIA_MAX_COUNT]:
            sections.extend(_format_critical_criterion(criterion))
    if review_phase.get("review_depth"):
        sections.append(f"**Review Depth Required:** {review_phase['review_depth']}\n")
    return sections


def _format_config_preferences_guidance(
    preferences: list[Any] | None,
) -> list[str]:
    items = _guidance_items(preferences)
    if not items:
        return []
    sections = ["**Preferences (a good idea should satisfy):**\n"]
    sections.extend(f"- {p}\n" for p in items)
    return sections


def _format_config_review_instructions_guidance(
    review_instructions: list[Any] | None,
) -> list[str]:
    items = _guidance_items(review_instructions)
    if not items:
        return []
    sections = ["\n**Review instructions (validate, do not restate the preferences):**\n"]
    sections.extend(f"- {r}\n" for r in items)
    return sections


def _format_config_attributes_guidance(
    attributes: list[Any] | None,
) -> list[str]:
    if not attributes:
        return []
    sections = ["\n**Stratification attributes (score each 1-5):**\n"]
    for attr in attributes:
        if isinstance(attr, dict) and attr.get("name"):
            sections.append(f"- {attr['name']}: {attr.get('rubric', '')}\n")
    return sections


def _format_config_synthesis_guidance(config: Any, *, needs_header: bool) -> list[str]:
    if not isinstance(config, dict):
        return []

    preferences = config.get("preferences")
    review_instructions = config.get("review_instructions")
    attributes = config.get("attributes")

    sections = []
    if needs_header and any((preferences, review_instructions, attributes)):
        sections.append("## Supervisor Guidance for Review\n")
    sections.extend(_format_config_preferences_guidance(preferences))
    sections.extend(_format_config_review_instructions_guidance(review_instructions))
    sections.extend(_format_config_attributes_guidance(attributes))
    return sections


def _format_supervisor_guidance_for_review(
    supervisor_guidance: dict[str, Any] | None,
) -> str:
    if not supervisor_guidance or not isinstance(supervisor_guidance, dict):
        return ""

    workflow_plan = supervisor_guidance.get("workflow_plan", {})
    sections = _format_review_phase_guidance(workflow_plan.get("review_phase", {}))

    config = supervisor_guidance.get("config_synthesis", {})
    sections.extend(_format_config_synthesis_guidance(config, needs_header=not sections))

    return "".join(sections)


def get_reflection_prompt(
    articles_with_reasoning: str,
    hypothesis_text: str,
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "reflection_observations",
        {
            "articles_with_reasoning": articles_with_reasoning,
            "hypothesis": hypothesis_text,
        },
        sections=PromptSections(meta_review_context=_format_meta_review_context(ctx.meta_review)),
        tool_registry=ctx.tool_registry,
    )
