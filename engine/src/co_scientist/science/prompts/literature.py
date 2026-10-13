from dataclasses import dataclass
from typing import Any

from co_scientist.science.prompts._common import (
    _format_bullet_list,
    _format_csv_list,
    _format_meta_review_context,
)
from co_scientist.science.prompts.context_budget import select_evidence_excerpt
from co_scientist.science.prompts.generation_draft import (
    _build_citation_reference_section,
    build_tool_instructions,
    format_articles_metadata,
)
from co_scientist.science.prompts.loading import _build_prompt, load_prompt
from co_scientist.science.prompts.untrusted import untrusted_evidence


@dataclass(frozen=True)
class LiteratureQueryInputs:
    preferences: str | None = None
    attributes: list[str] | None = None
    user_literature: list[str] | None = None
    user_hypotheses: list[str] | None = None


# Goal queries explore broadly; hypothesis queries seek confirming and refuting
# evidence.
def get_literature_review_query_generation_prompt(
    research_goal: str,
    source_type: str = "academic",
    inputs: LiteratureQueryInputs | None = None,
    meta_review: dict[str, Any] | None = None,
) -> str:
    inputs = inputs or LiteratureQueryInputs()
    template_name = {
        "pubmed": "literature_review_query_generation_pubmed",
    }.get(source_type, "literature_review_query_generation_generic")
    return load_prompt(
        template_name,
        {
            "research_goal": research_goal,
            "preferences": inputs.preferences or "None provided",
            "attributes": _format_csv_list(inputs.attributes),
            "user_literature": _format_bullet_list(inputs.user_literature),
            "user_hypotheses": _format_bullet_list(inputs.user_hypotheses),
            "meta_review_context": _format_meta_review_context(meta_review),
        },
    )


# Hypothesis queries preserve the idea's own entities rather than broadening to
# the run goal.
def get_hypothesis_query_generation_prompt(
    research_goal: str,
    hypothesis: str,
) -> str:
    return load_prompt(
        "hypothesis_query_generation",
        {"research_goal": research_goal, "hypothesis": hypothesis},
    )


def get_literature_review_paper_analysis_prompt(
    research_goal: str,
    title: str,
    authors: list[str],
    year: int | None,
    fulltext: str,
) -> str:
    return load_prompt(
        "literature_review_paper_analysis",
        {
            "research_goal": research_goal,
            "paper_evidence": untrusted_evidence(
                "retrieved paper",
                {
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "fulltext": select_evidence_excerpt(fulltext, research_goal, 16_000),
                },
            ),
        },
    )


def get_literature_review_relevance_batch_prompt(
    research_goal: str,
    candidates_block: str,
) -> str:
    return load_prompt(
        "literature_review_relevance_batch",
        {
            "research_goal": research_goal,
            "candidates_block": candidates_block,
        },
    )


def _format_paper_analyses(paper_analyses: list[dict[str, Any]]) -> str:
    analyses_text = []
    for i, analysis_data in enumerate(paper_analyses, 1):
        metadata = analysis_data.get("metadata", {})
        analysis = analysis_data.get("analysis", {})

        paper_section = f"""### paper {i}: {metadata.get("title", "Unknown")}
**authors:** {", ".join(metadata.get("authors", ["Unknown"]))}
**year:** {metadata.get("year", "Unknown")}

**key findings:** {analysis.get("key_findings", "N/A")}

**gaps identified:** {analysis.get("gaps_identified", "N/A")}

**future work suggested:** {analysis.get("future_work", "N/A")}

**methodology limitations:** {analysis.get("methodology_limitations", "N/A")}

**unexplored areas:** {analysis.get("unexplored_areas", "N/A")}

**relevance:** {analysis.get("relevance", "N/A")}
"""
        analyses_text.append(
            untrusted_evidence(
                "derived paper analysis",
                {"paper_id": analysis_data.get("paper_id"), "analysis": paper_section},
            )
        )

    return "\n\n".join(analyses_text)


def get_literature_review_synthesis_prompt(
    research_goal: str,
    paper_analyses: list[dict[str, Any]],
    background_context: str = "",
    meta_review: dict[str, Any] | None = None,
) -> str:
    background_context_section = (
        "\n## Mechanistic Background (Knowledge Graph)\n\n"
        "The following structured evidence was retrieved from external"
        " knowledge sources "
        "to supplement the literature. Use it to ground the synthesis"
        " in known causal "
        "relationships and flag where hypotheses can leverage or"
        " contradict this background.\n\n"
        + untrusted_evidence("retrieved knowledge graph", background_context)
        if background_context
        else ""
    )

    return load_prompt(
        "literature_review_synthesis",
        {
            "research_goal": research_goal,
            "paper_analyses": _format_paper_analyses(paper_analyses),
            "background_context_section": background_context_section,
            "meta_review_context": _format_meta_review_context(meta_review),
        },
    )


def get_hypothesis_novelty_analysis_prompt(
    hypothesis_text: str,
    title: str,
    authors: list[str],
    year: int | None,
    fulltext: str,
) -> str:
    return load_prompt(
        "hypothesis_novelty_analysis",
        {
            "hypothesis_text": untrusted_evidence("draft hypothesis", hypothesis_text),
            "paper_evidence": untrusted_evidence(
                "retrieved paper",
                {"title": title, "authors": authors, "year": year, "fulltext": fulltext},
            ),
        },
    )


def _format_novelty_paper_analysis(j: int, analysis_data: dict[str, Any]) -> str:
    paper_meta = analysis_data.get("paper_metadata", {})
    analysis = analysis_data.get("analysis", {})
    p_title = paper_meta.get("title", "Unknown")
    p_year = paper_meta.get("year", "N/A")

    return (
        f"\n**paper {j}:** {p_title} ({p_year})\n"
        f"- methods used:"
        f" {analysis.get('methods_used', 'N/A')}\n"
        f"- populations studied:"
        f" {analysis.get('populations_studied', 'N/A')}\n"
        f"- mechanisms investigated:"
        f" {analysis.get('mechanisms_investigated', 'N/A')}\n"
        f"- key findings:"
        f" {analysis.get('key_findings', 'N/A')}\n"
        f"- stated limitations:"
        f" {analysis.get('stated_limitations', 'N/A')}\n"
        f"- future work suggested:"
        f" {analysis.get('future_work_suggested', 'N/A')}\n"
        f"- **novelty assessment:"
        f" {analysis.get('novelty_assessment', 'N/A')}**\n"
        f"- overlap explanation:"
        f" {analysis.get('overlap_explanation', 'N/A')}\n"
    )


def _format_novelty_hypothesis_section(i: int, hyp_data: dict[str, Any]) -> str:
    draft = hyp_data.get("draft", {})
    analyses = hyp_data.get("novelty_analyses", [])

    hyp_section = f"""### draft hypothesis {i}
**text:** {draft.get("text", "Unknown")}
**gap reasoning:** {draft.get("gap_reasoning", "N/A")}
**literature sources:** {draft.get("literature_sources", "N/A")}

**novelty analyses ({len(analyses)} papers examined):**
"""

    for j, analysis_data in enumerate(analyses, 1):
        hyp_section += _format_novelty_paper_analysis(j, analysis_data)

    return hyp_section


def _format_hypotheses_with_novelty_analyses(
    hypotheses_with_analyses: list[dict[str, Any]],
) -> str:
    hypotheses_text = [
        _format_novelty_hypothesis_section(i, hyp_data)
        for i, hyp_data in enumerate(hypotheses_with_analyses, 1)
    ]
    return untrusted_evidence("draft hypotheses and derived novelty analyses", hypotheses_text)


def _build_already_validated_context(
    already_validated_texts: list[str] | None,
) -> str:
    if not already_validated_texts:
        return ""
    lines = untrusted_evidence("previous validated hypotheses", already_validated_texts)
    return f"""
## Hypotheses Already Validated (Diversity Constraint)

The following hypotheses have already been validated and will be included in \
the final output.
Your output **must explore different mechanistic territory** from each of these.
If your draft overlaps significantly with any entry below, treat it as \
saturated and pivot:

{lines}

"""


def _resolve_validation_tool_instructions(tool_registry: Any | None) -> str:
    tool_ids = []
    if tool_registry:
        tool_ids = tool_registry.get_tools_for_workflow("validation")
    return build_tool_instructions(tool_ids, tool_registry)


@dataclass(frozen=True)
class ValidationSynthesisRequest:
    research_goal: str
    hypotheses_with_analyses: list[dict[str, Any]]
    articles: list[Any] | None = None
    articles_with_reasoning: str | None = None
    max_iterations: int = 8
    tool_registry: Any | None = None
    reference_list: str = ""
    already_validated_texts: list[str] | None = None


def _build_validation_synthesis_prompt_variables(
    req: ValidationSynthesisRequest,
) -> dict[str, Any]:
    return {
        "research_goal": req.research_goal,
        "hypotheses_with_analyses": _format_hypotheses_with_novelty_analyses(
            req.hypotheses_with_analyses
        ),
        "hypotheses_count": len(req.hypotheses_with_analyses),
        "articles_metadata": untrusted_evidence(
            "retrieved paper metadata", format_articles_metadata(req.articles or [])
        ),
        "articles_with_reasoning": untrusted_evidence(
            "derived literature summary",
            req.articles_with_reasoning or "no literature review summary available.",
        ),
        "citation_reference_section": _build_citation_reference_section(
            untrusted_evidence("citation provenance", req.reference_list)
            if req.reference_list
            else ""
        ),
        "max_iterations": req.max_iterations,
        "tool_instructions": _resolve_validation_tool_instructions(req.tool_registry),
        "already_validated_context": _build_already_validated_context(req.already_validated_texts),
    }


def get_validation_synthesis_prompt_with_tools(
    req: ValidationSynthesisRequest,
) -> tuple[str, dict[str, Any] | None]:
    return _build_prompt(
        "hypothesis_validation_synthesis_with_tools",
        _build_validation_synthesis_prompt_variables(req),
        tool_registry=req.tool_registry,
    )
