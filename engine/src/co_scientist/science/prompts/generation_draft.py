from dataclasses import dataclass, field
from typing import Any

from co_scientist.science.prompts._common import (
    PromptRunContext,
    PromptSections,
    _format_meta_review_context,
    _run_guidance_section,
    format_lab_constraints_section,
)
from co_scientist.science.prompts.context_budget import (
    bounded_excerpt,
    select_evidence_excerpt,
    summarize_references,
)
from co_scientist.science.prompts.loading import _build_prompt
from co_scientist.science.prompts.untrusted import untrusted_evidence

from ._common import _csv_value, _guidance_items


def format_preferences(preferences: str | None) -> str:
    if preferences:
        return preferences
    return "Focus on novelty, testability, and potential impact."


def format_attributes(attributes: list[str] | None) -> str:
    if attributes:
        return "\n".join(f"- {attr}" for attr in attributes)
    return "- Novel\n- Testable\n- Impactful"


def format_user_hypotheses(user_hypotheses: list[str] | None) -> str:
    if user_hypotheses:
        return "\n".join(f"- {hyp}" for hyp in user_hypotheses)
    return "No user-provided starting hypotheses."


# Draft and debate writers receive different authored config guidance.
_MODE_INSTRUCTION_HEADERS = {
    "draft_instructions": "\n**How to draft in this run:**\n",
    "debate_instructions": "\n**How to run the debate in this run:**\n",
}


def _format_guidance_bullets(header: str, value: Any) -> list[str]:
    items = _guidance_items(value)
    if not items:
        return []
    return [header, *(f"- {item}\n" for item in items)]


def format_config_generation_guidance(
    supervisor_guidance: dict[str, Any], instructions_key: str
) -> list[str]:
    """Draft and debate writers receive different authored guidance for their
    respective jobs.
    """
    config = supervisor_guidance.get("config_synthesis")
    if not isinstance(config, dict):
        return []
    sections = _format_guidance_bullets(
        "**Preferences (a good idea should satisfy):**\n",
        config.get("preferences"),
    )
    sections.extend(
        _format_guidance_bullets(
            _MODE_INSTRUCTION_HEADERS[instructions_key],
            config.get(instructions_key),
        )
    )
    return sections


def format_supervisor_guidance_for_generation(
    supervisor_guidance: dict[str, Any] | None,
) -> str:
    if not isinstance(supervisor_guidance, dict) or not supervisor_guidance:
        return ""

    # json_object mode does not enforce nested types; guard before chained
    # lookups.
    workflow_plan = supervisor_guidance.get("workflow_plan")
    generation_phase = (
        workflow_plan.get("generation_phase") if isinstance(workflow_plan, dict) else None
    )
    sections = []
    if isinstance(generation_phase, dict) and generation_phase.get("focus_areas"):
        focus = _csv_value(generation_phase["focus_areas"])
        sections.append(f"**Focus on:** {focus}\n\n")
    sections.extend(format_config_generation_guidance(supervisor_guidance, "draft_instructions"))
    if not sections:
        return ""
    return "## Supervisor Guidance for Generation\n\n" + "".join(sections)


def _format_pdf_status(article: Any) -> str:
    if article.pdf_links:
        return f"Available - {article.pdf_links[0]}"
    return "No PDF found (abstract only)"


def _used_articles(articles: list[Any]) -> list[Any]:
    return [art for art in articles if art.used_in_analysis]


def _format_article_authors(article: Any) -> str:
    authors = ", ".join(article.authors[:3])
    if len(article.authors) > 3:
        return f"{authors} et al."
    return authors


def _format_article_entry(index: int, article: Any) -> str:
    return (
        f"**{index + 1}. {article.title}**\n"
        f"   - Authors: {_format_article_authors(article)}\n"
        f"   - Year: {article.year or 'Unknown'}\n"
        f"   - Citations: {article.citations}\n"
        f"   - PDF: {_format_pdf_status(article)}\n"
        f"   - URL: {article.url}"
    )


def format_articles_metadata(articles: list[Any]) -> str:
    if not articles:
        return ""

    used_articles = _used_articles(articles)
    if not used_articles:
        return ""

    articles_list_text = "\n\n".join(
        _format_article_entry(i, art) for i, art in enumerate(used_articles)
    )

    n = len(used_articles)
    return (
        "\n### Papers Analyzed in Literature Review\n\n"
        f"These {n} papers were ranked highest and analyzed."
        " Some may have had accessibility issues"
        " (abstracts only, paywalls, captchas).\n"
        "You can use tools to:\n"
        "- Try accessing PDFs that weren't available initially\n"
        "- Query specific papers for detailed information\n"
        "- Search for alternative papers if these have issues\n"
        f"\n{articles_list_text}\n"
    )


def _build_citation_reference_section(reference_list: str) -> str:
    if not reference_list.strip():
        return ""
    return (
        "\n## Citation Reference List\n\n"
        "Use **only** these `[C*]` citation keys inline in"
        " `literature_grounding` "
        "— do NOT invent author-year citations.\n\n" + reference_list + "\n"
    )


def _format_tool_entry(tool_config: Any) -> list[str]:
    """YAML prompt snippets are authored runtime guidance; retain their
    ordering and indentation.
    """
    lines = [f"- `{tool_config.mcp_tool_name}`: {tool_config.description}"]

    if tool_config.prompt_snippet:
        snippet_lines = tool_config.prompt_snippet.strip().split("\n")
        lines.extend(f"  {line}" for line in snippet_lines)

    lines.append("")
    return lines


def _resolve_tool_registry(
    tool_ids: list[str],
    tool_registry: Any | None,
) -> tuple[Any | None, list[str]]:
    if tool_registry is None:
        try:
            from co_scientist.platform.retrieval.config import (
                get_tool_registry,
            )

            tool_registry = get_tool_registry()
            if not tool_ids:
                tool_ids = tool_registry.get_tools_for_workflow("draft_generation")
        except Exception:
            pass

    return tool_registry, tool_ids


def _tool_entry_sections(tool_id: str, tool_registry: Any) -> list[str]:
    tool_config = tool_registry.get_tool(tool_id)
    if not tool_config or not tool_config.enabled:
        return []
    return _format_tool_entry(tool_config)


def build_tool_instructions(
    tool_ids: list[str],
    tool_registry: Any | None = None,
) -> str:
    tool_registry, tool_ids = _resolve_tool_registry(tool_ids, tool_registry)

    if not tool_registry or not tool_ids:
        return "No tool configuration available. Literature tools may not be accessible."

    sections = []
    for tool_id in tool_ids:
        sections.extend(_tool_entry_sections(tool_id, tool_registry))

    if not sections:
        return "No tools available."

    return "\n".join(sections).strip()


def _resolve_draft_tool_instructions(tool_registry: Any | None) -> str:
    tool_ids = []
    if tool_registry:
        tool_ids = tool_registry.get_tools_for_workflow("draft_generation")
    return build_tool_instructions(tool_ids, tool_registry)


@dataclass(frozen=True)
class DraftPromptRequest:
    research_goal: str
    hypotheses_count: int
    articles: list[Any] | None = None
    articles_with_reasoning: str | None = None
    preferences: str | None = None
    attributes: list[str] | None = None
    user_hypotheses: list[str] | None = None
    instructions: str | None = None
    max_iterations: int = 8
    reference_list: str = ""
    research_expansion_section: str = ""
    falsified_assumptions_section: str = ""
    lab_constraints: list[str] | None = None
    skills_section: str = ""
    context: PromptRunContext = field(default_factory=PromptRunContext)


def _build_draft_prompt_variables(req: DraftPromptRequest) -> dict[str, Any]:
    return {
        "goal": req.research_goal,
        "hypotheses_count": req.hypotheses_count,
        "preferences": format_preferences(req.preferences),
        "attributes": format_attributes(req.attributes),
        "user_hypotheses": format_user_hypotheses(req.user_hypotheses),
        "supervisor_guidance": format_supervisor_guidance_for_generation(
            req.context.supervisor_guidance
        ),
        "articles_with_reasoning": untrusted_evidence(
            "derived literature summary",
            select_evidence_excerpt(req.articles_with_reasoning, req.research_goal, 6_000)
            if req.articles_with_reasoning
            else "no literature review summary available - examine papers below directly.",
        ),
        "articles_metadata": untrusted_evidence(
            "retrieved paper metadata",
            ""
            if req.reference_list
            else bounded_excerpt(format_articles_metadata(req.articles or []), 2_000),
        ),
        "citation_reference_section": _build_citation_reference_section(
            untrusted_evidence("citation provenance", summarize_references(req.reference_list))
            if req.reference_list
            else ""
        ),
        "max_iterations": req.max_iterations,
        "instructions": req.instructions
        or "Focus on creative ideation - draft diverse hypotheses based on literature gaps.",
        "tool_instructions": _resolve_draft_tool_instructions(req.context.tool_registry)
        + req.skills_section,
        # Empty outside their conditions, preserving the initial-cycle prompt.
        "research_expansion_section": req.research_expansion_section,
        "falsified_assumptions_section": req.falsified_assumptions_section,
        "lab_constraints_section": format_lab_constraints_section(req.lab_constraints),
    }


def get_draft_prompt_with_tools(
    req: DraftPromptRequest,
) -> tuple[str, dict[str, Any] | None]:
    return _build_prompt(
        "generation_draft_with_tools",
        _build_draft_prompt_variables(req),
        sections=PromptSections(
            meta_review_context=_format_meta_review_context(req.context.meta_review),
            run_guidance=_run_guidance_section(req.context),
        ),
        tool_registry=req.context.tool_registry,
    )
