from dataclasses import dataclass
from typing import Any

from co_scientist.science.prompts._common import (
    PromptRunContext,
    PromptSections,
    _csv_value,
    _format_bullet_list,
    _format_bullet_section,
    _format_csv_list,
    _format_meta_review_context,
    _run_guidance_section,
)
from co_scientist.science.prompts.context_budget import (
    select_evidence_excerpt,
    summarize_feedback,
    summarize_hypotheses,
)
from co_scientist.science.prompts.generation_draft import format_preferences
from co_scientist.science.prompts.loading import _build_prompt

_NO_CORPUS = "No verified evidence corpus available."


def get_knowledge_base_outline_prompt(
    research_goal: str,
    hypotheses_summary: str,
    evidence_corpus: str = _NO_CORPUS,
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "research_overview_knowledge_base_outline",
        {
            "research_goal": research_goal,
            "hypotheses_summary": summarize_hypotheses(hypotheses_summary),
            "evidence_corpus": select_evidence_excerpt(evidence_corpus, research_goal),
        },
        sections=PromptSections(run_guidance=_run_guidance_section(ctx)),
        tool_registry=ctx.tool_registry,
    )


@dataclass(frozen=True)
class ThemeWritingMaterial:
    """Each independent writer sees the whole outline to avoid overlapping
    passages.
    """

    title: str
    sections: str
    outline: str


def get_knowledge_base_theme_prompt(
    research_goal: str,
    material: ThemeWritingMaterial,
    evidence_corpus: str = _NO_CORPUS,
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "research_overview_knowledge_base_theme",
        {
            "research_goal": research_goal,
            "theme_title": material.title,
            "theme_sections": material.sections,
            "outline": material.outline,
            "evidence_corpus": select_evidence_excerpt(evidence_corpus, research_goal),
        },
        sections=PromptSections(run_guidance=_run_guidance_section(ctx)),
        tool_registry=ctx.tool_registry,
    )


_NO_CORPUS = "No verified evidence corpus available."


@dataclass(frozen=True)
class DirectionWritingMaterial:
    """Concurrent writers see every direction title to avoid arguing the same
    mechanism twice.
    """

    title: str
    rationale: str
    all_directions: str


def get_research_overview_direction_prompt(
    research_goal: str,
    material: DirectionWritingMaterial,
    hypotheses_summary: str,
    evidence_corpus: str = _NO_CORPUS,
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "research_overview_direction",
        {
            "research_goal": research_goal,
            "direction_title": material.title,
            "direction_rationale": material.rationale,
            "all_directions": material.all_directions,
            "hypotheses_summary": summarize_hypotheses(hypotheses_summary),
            "evidence_corpus": select_evidence_excerpt(evidence_corpus, research_goal),
        },
        sections=PromptSections(run_guidance=_run_guidance_section(ctx)),
        tool_registry=ctx.tool_registry,
    )


# No state value supplies this unconditional template slot; render a truthful
# absence.
_NO_META_REVIEW_INSTRUCTIONS = "No additional instructions were supplied for this synthesis."


def get_meta_review_prompt(
    research_goal: str,
    all_reviews: str,
    instructions: str | None = None,
    preferences: str | None = None,
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "meta_review",
        {
            "research_goal": research_goal,
            "all_reviews": summarize_feedback(all_reviews),
            "instructions": instructions or _NO_META_REVIEW_INSTRUCTIONS,
            "preferences": format_preferences(preferences),
        },
        sections=PromptSections(
            supervisor_guidance=_format_supervisor_guidance_for_meta_review(
                ctx.supervisor_guidance
            ),
            run_guidance=_run_guidance_section(ctx),
        ),
        tool_registry=ctx.tool_registry,
    )


def get_research_overview_prompt(
    research_goal: str,
    hypotheses_summary: str,
    contact_candidates: str = "No verified literature authors available.",
    evidence_corpus: str = "No verified evidence corpus available.",
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "research_overview",
        {
            "research_goal": research_goal,
            "hypotheses_summary": summarize_hypotheses(hypotheses_summary),
            "contact_candidates": contact_candidates,
            "evidence_corpus": select_evidence_excerpt(evidence_corpus, research_goal),
        },
        sections=PromptSections(
            meta_review_context=_format_meta_review_context(ctx.meta_review),
            run_guidance=_run_guidance_section(ctx),
        ),
        tool_registry=ctx.tool_registry,
    )


# Periodic synthesis writes only direction titles and questions consumed by the
# next cycle.
def get_research_overview_interim_prompt(
    research_goal: str,
    hypotheses_summary: str,
    evidence_corpus: str = "No verified evidence corpus available.",
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "research_overview_interim",
        {
            "research_goal": research_goal,
            "hypotheses_summary": summarize_hypotheses(hypotheses_summary),
            "evidence_corpus": select_evidence_excerpt(evidence_corpus, research_goal),
        },
        sections=PromptSections(
            meta_review_context=_format_meta_review_context(ctx.meta_review),
            run_guidance=_run_guidance_section(ctx),
        ),
        tool_registry=ctx.tool_registry,
    )


@dataclass(frozen=True)
class OverviewReviewMaterial:
    research_goal: str
    hypotheses_summary: str
    evidence_corpus: str


def get_research_overview_review_prompt(
    material: OverviewReviewMaterial,
    drafted_overview: str,
) -> tuple[str, dict[str, Any] | None]:
    return _build_prompt(
        "research_overview_review",
        {
            "research_goal": material.research_goal,
            "hypotheses_summary": summarize_hypotheses(material.hypotheses_summary),
            "evidence_corpus": select_evidence_excerpt(
                material.evidence_corpus, material.research_goal
            ),
            "drafted_overview": drafted_overview,
        },
        include_domain=False,
    )


@dataclass(frozen=True)
class OverviewRevisionRequest:
    material: OverviewReviewMaterial
    contact_candidates: str
    drafted_overview: str
    review_notes: str


def get_research_overview_revise_prompt(
    request: OverviewRevisionRequest,
) -> tuple[str, dict[str, Any] | None]:
    material = request.material
    return _build_prompt(
        "research_overview_revise",
        {
            "research_goal": material.research_goal,
            "hypotheses_summary": summarize_hypotheses(material.hypotheses_summary),
            "contact_candidates": request.contact_candidates,
            "evidence_corpus": select_evidence_excerpt(
                material.evidence_corpus, material.research_goal
            ),
            "drafted_overview": request.drafted_overview,
            "review_notes": request.review_notes,
        },
        include_domain=False,
    )


def _format_lit_review_description(mcp_available: bool, pubmed_available: bool) -> str:
    if pubmed_available or mcp_available:
        return "literature review will search pubmed for relevant papers and analyze them"
    return "literature review is not available (no pubmed access)"


@dataclass(frozen=True)
class SupervisorPromptInputs:
    research_goal: str
    preferences: str | None = None
    attributes: list[str] | None = None
    constraints: list[str] | None = None
    criteria: list[str] | None = None
    user_hypotheses: list[str] | None = None
    user_literature: list[str] | None = None
    initial_hypotheses_count: int | None = None
    max_iterations: int | None = None
    evolution_max_count: int | None = None
    mcp_available: bool = False
    pubmed_available: bool = False


def _build_supervisor_prompt_variables(
    inputs: SupervisorPromptInputs,
) -> dict[str, Any]:
    """Normalize absent inputs so raw Python None never reaches a template."""
    return {
        "research_goal": inputs.research_goal,
        "preferences": inputs.preferences or "None provided",
        "attributes": _format_csv_list(inputs.attributes),
        "constraints": _format_bullet_list(inputs.constraints),
        "criteria": _format_bullet_list(inputs.criteria),
        "user_hypotheses": _format_bullet_list(inputs.user_hypotheses),
        "user_literature": _format_bullet_list(inputs.user_literature),
        "initial_hypotheses_count": (inputs.initial_hypotheses_count or "not specified"),
        "max_iterations": inputs.max_iterations or "not specified",
        "evolution_max_count": inputs.evolution_max_count or "not specified",
        "literature_review_description": _format_lit_review_description(
            inputs.mcp_available, inputs.pubmed_available
        ),
    }


def get_supervisor_prompt(
    inputs: SupervisorPromptInputs,
    context: PromptRunContext | None = None,
) -> tuple[str, dict[str, Any] | None]:
    ctx = context or PromptRunContext()
    return _build_prompt(
        "supervisor",
        _build_supervisor_prompt_variables(inputs),
        sections=PromptSections(run_guidance=_run_guidance_section(ctx)),
        tool_registry=ctx.tool_registry,
    )


def _format_meta_review_evolution_phase_section(
    evolution_phase: dict[str, Any],
) -> list[str]:
    if not evolution_phase:
        return []

    sections = ["**Evolution Phase Guidance:**\n"]
    if evolution_phase.get("refinement_priorities"):
        priorities = _csv_value(evolution_phase["refinement_priorities"])
        sections.append(f"- Refinement Priorities: {priorities}\n")
    if evolution_phase.get("iteration_strategy"):
        iter_strat = evolution_phase["iteration_strategy"]
        sections.append(f"- Iteration Strategy: {iter_strat}\n")
    sections.append("\n")
    return sections


def _format_supervisor_guidance_for_meta_review(
    supervisor_guidance: dict[str, Any] | None,
) -> str:
    if not supervisor_guidance or not isinstance(supervisor_guidance, dict):
        return ""

    goal_analysis = supervisor_guidance.get("research_goal_analysis", {})
    workflow_plan = supervisor_guidance.get("workflow_plan", {})

    sections = ["## Supervisor Guidance\n"]
    sections.append(
        _format_bullet_section("Key Research Areas", goal_analysis.get("key_areas", []))
    )
    sections.extend(
        _format_meta_review_evolution_phase_section(workflow_plan.get("evolution_phase", {}))
    )
    sections.append(
        "Use this guidance to ensure your meta-review synthesis aligns"
        " with the research plan and evolution strategy.\n"
    )

    return "".join(sections)
