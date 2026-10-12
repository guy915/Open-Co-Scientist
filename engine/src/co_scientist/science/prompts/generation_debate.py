from dataclasses import dataclass, field
from typing import Any, Final

from co_scientist.science.prompts._common import (
    PromptRunContext,
    _csv_value,
    _format_meta_review_context,
    _run_guidance_section,
)
from co_scientist.science.prompts.context_budget import (
    bounded_excerpt,
    select_evidence_excerpt,
    summarize_references,
    summarize_transcript,
)
from co_scientist.science.prompts.generation_draft import (
    _build_citation_reference_section,
    format_articles_metadata,
    format_config_generation_guidance,
    format_user_hypotheses,
)
from co_scientist.science.prompts.loading import (
    _get_domain_variables,
    _read_prompt_template,
    load_prompt,
    load_prompt_with_schema,
)


def _format_debate_key_areas_section(key_areas: list[Any], *, needs_header: bool) -> list[str]:
    if not key_areas:
        return []

    sections = []
    if needs_header:
        sections.append("Key research areas to consider:\n")
    for area in key_areas:
        sections.append(f"- {area}\n")
    return sections


def _format_debate_generation_phase_section(
    generation_phase: dict[str, Any], *, needs_header: bool
) -> list[str]:
    if not generation_phase:
        return []

    sections = []
    if needs_header:
        sections.append("Generation guidance:\n")
    if generation_phase.get("focus_areas"):
        focus = _csv_value(generation_phase["focus_areas"])
        sections.append(f"Focus on: {focus}\n")
    return sections


def _format_supervisor_guidance_for_debate(
    supervisor_guidance: dict[str, Any] | None,
) -> str:
    if not supervisor_guidance or not isinstance(supervisor_guidance, dict):
        return ""

    goal_analysis = supervisor_guidance.get("research_goal_analysis", {})
    workflow_plan = supervisor_guidance.get("workflow_plan", {})
    generation_phase = workflow_plan.get("generation_phase", {})

    sections = _format_debate_key_areas_section(
        goal_analysis.get("key_areas", []), needs_header=True
    )
    sections.extend(
        _format_debate_generation_phase_section(generation_phase, needs_header=not sections)
    )
    config_sections = format_config_generation_guidance(supervisor_guidance, "debate_instructions")
    if config_sections:
        if sections:
            sections.append("\n")
        sections.extend(config_sections)

    return "".join(sections)


# WorkflowState has no instructions field; the default prevents MISSING in the
# template.
_DEFAULT_DEBATE_INSTRUCTIONS = (
    "Focus on creative ideation - debate diverse hypotheses, building on"
    " the user-provided starting hypotheses above when present."
)


def _format_debate_attributes(attributes: str | list[str] | None) -> str:
    if isinstance(attributes, list):
        return ", ".join(attributes) or "testable and falsifiable"
    return attributes or "testable and falsifiable"


def _build_debate_literature_variables(
    articles_with_reasoning: str | None,
    articles: list[Any] | None,
    reference_list: str,
) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "articles_metadata": format_articles_metadata(articles or []),
        "citation_reference_section": _build_citation_reference_section(
            summarize_references(reference_list or "")
        ),
    }
    if articles_with_reasoning:
        variables["articles_with_reasoning"] = articles_with_reasoning
    return variables


@dataclass(frozen=True)
class DebatePromptRequest:
    research_goal: str
    transcript: str
    preferences: str | None = None
    attributes: str | list[str] | None = None
    user_hypotheses: list[str] | None = None
    instructions: str | None = None
    criteria: list[str] | None = None
    is_final_turn: bool = False
    articles_with_reasoning: str | None = None
    articles: list[Any] | None = None
    reference_list: str = ""
    context: PromptRunContext = field(default_factory=PromptRunContext)


# Published debates typically take 3-5 turns, with ten discussion turns allowed.
# Loop/template limits agree; synthesis is outside the discussion allowance.
_DEBATE_TYPICAL_MIN_TURNS: Final = 3

_DEBATE_TYPICAL_MAX_TURNS: Final = 5

_DEBATE_MAX_DISCUSSION_TURNS: Final = 10


def _debate_turn_envelope() -> dict[str, int]:
    return {
        "discussion_typical_min_turns": _DEBATE_TYPICAL_MIN_TURNS,
        "discussion_typical_max_turns": _DEBATE_TYPICAL_MAX_TURNS,
        "discussion_max_turns": _DEBATE_MAX_DISCUSSION_TURNS,
    }


def _format_debate_evaluation_criteria(
    criteria: list[str] | None,
) -> str:
    cleaned = [str(item).strip() for item in criteria or [] if str(item).strip()]
    if not cleaned:
        return ""

    sections = [
        "## Scientist Evaluation Criteria\n",
        "The scientist who commissioned this research specified how the "
        "outcome will be judged. Let these criteria steer the debate -- "
        "weigh arguments, refinements, and the final hypothesis against "
        "them:\n",
    ]
    sections.extend(f"- {item}\n" for item in cleaned)
    sections.append("\n")
    return "".join(sections)


def _build_debate_base_variables(req: DebatePromptRequest) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "goal": req.research_goal,
        "transcript": summarize_transcript(req.transcript or ""),
        "preferences": req.preferences
        or "Novel, testable, scientifically sound, specific, and diverse hypotheses",
        "attributes": _format_debate_attributes(req.attributes),
        "user_hypotheses": format_user_hypotheses(req.user_hypotheses),
        "instructions": req.instructions or _DEFAULT_DEBATE_INSTRUCTIONS,
        # Always supply the slot, even when empty, to avoid MISSING.
        "evaluation_criteria": _format_debate_evaluation_criteria(req.criteria),
    }
    variables.update(_debate_turn_envelope())
    return variables


def _format_reviews_overview(meta_review: dict[str, Any] | None) -> str:
    return _format_meta_review_context(meta_review).strip() or (
        "No reviews are available yet; this is the first generation cycle of the run."
    )


def _build_debate_guidance_variables(
    context: PromptRunContext,
) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "supervisor_guidance": _format_supervisor_guidance_for_debate(context.supervisor_guidance),
        "reviews_overview": _format_reviews_overview(context.meta_review),
        "run_guidance": _run_guidance_section(context),
    }
    variables.update(_get_domain_variables(context.tool_registry))
    return variables


def _build_debate_prompt_variables(
    req: DebatePromptRequest,
) -> dict[str, Any]:
    variables = _build_debate_base_variables(req)
    variables.update(
        _build_debate_literature_variables(
            req.articles_with_reasoning, req.articles, req.reference_list
        )
    )
    variables.update(_build_debate_guidance_variables(req.context))
    if req.articles_with_reasoning:
        variables["articles_with_reasoning"] = select_evidence_excerpt(
            req.articles_with_reasoning, req.research_goal, 6_000
        )
    # The reference list already identifies these same papers.
    variables["articles_metadata"] = (
        "" if req.reference_list else bounded_excerpt(str(variables["articles_metadata"]), 2_000)
    )
    return variables


def _render_debate_prompt(
    variables: dict[str, Any],
    articles_with_reasoning: str | None,
    is_final_turn: bool,
) -> tuple[str, dict[str, Any] | None]:
    prompt_name = (
        "generation_debate_and_literature" if articles_with_reasoning else "generation_after_debate"
    )
    if not is_final_turn:
        return load_prompt(prompt_name, variables), None

    prompt, schema = load_prompt_with_schema(prompt_name, variables)
    # Append verbatim: this instruction contains literal JSON braces that must
    # not be substituted.
    prompt = prompt + _read_prompt_template("generation_debate_final")
    return prompt, schema


def get_debate_generation_prompt(
    req: DebatePromptRequest,
) -> tuple[str, dict[str, Any] | None]:
    return _render_debate_prompt(
        _build_debate_prompt_variables(req),
        req.articles_with_reasoning,
        req.is_final_turn,
    )
