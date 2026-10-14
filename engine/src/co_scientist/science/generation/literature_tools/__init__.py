import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from co_scientist.science.citations import (
        ReferenceIndex,
    )

from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.mcp_client import get_mcp_client
from co_scientist.science.generation.literature_tools.draft import (
    draft_hypotheses,
)
from co_scientist.science.generation.literature_tools.validate import (
    validate_hypotheses,
)

logger = logging.getLogger(__name__)


def _log_warm_start_diagnostics(articles: list[Any] | None) -> None:
    if not articles:
        return

    used_count = sum(1 for art in articles if art.used_in_analysis)
    logger.debug(
        "state.articles contains %s total articles, %s with used_in_analysis=True",
        len(articles),
        used_count,
    )
    if used_count > 0:
        articles_with_pdfs = sum(1 for art in articles if art.used_in_analysis and art.pdf_links)
        logger.info(
            "Including %s analyzed articles in prompt (%s with PDFs, %s abstract-only)",
            used_count,
            articles_with_pdfs,
            used_count - articles_with_pdfs,
        )
    else:
        logger.warning(
            "No articles with used_in_analysis=True found in state - agent will search fresh"
        )


async def _get_mcp_client_for_generation(
    tool_registry: Any | None,
) -> Any:
    try:
        return await get_mcp_client(tool_registry=tool_registry)
    except Exception as e:
        logger.warning("Failed to get MCP client: %s", e)
        raise


def _log_generated_hypothesis_methods(hypotheses: list[Hypothesis]) -> None:
    for i, hyp in enumerate(hypotheses):
        method = hyp.generation_method
        logger.debug(
            "tool-generated hypothesis %s: generation_method=%s, text=%s...",
            i + 1,
            method.value if method else None,
            hyp.text[:80],
        )


async def _run_draft_phase(
    state: WorkflowState,
    count: int,
    mcp_client: Any,
    tool_registry: Any | None,
    reference_index: Optional["ReferenceIndex"],
) -> tuple[list[dict[str, str]], int]:
    draft_hyps, llm_calls = await draft_hypotheses(
        state=state,
        count=count,
        mcp_client=mcp_client,
        tool_registry=tool_registry,
        reference_index=reference_index,
    )
    logger.info("Phase 1 complete: drafted %s hypotheses", len(draft_hyps))
    return draft_hyps, llm_calls


async def _run_validate_phase(
    state: WorkflowState,
    draft_hyps: list[dict[str, str]],
    mcp_client: Any,
    tool_registry: Any | None,
    reference_index: Optional["ReferenceIndex"],
) -> tuple[list[Hypothesis], int]:
    hypotheses, llm_calls = await validate_hypotheses(
        state=state,
        draft_hypotheses=draft_hyps,
        mcp_client=mcp_client,
        tool_registry=tool_registry,
        reference_index=reference_index,
    )
    logger.info("Phase 2 complete: validated %s hypotheses", len(hypotheses))
    return hypotheses, llm_calls


async def generate_with_tools(
    state: WorkflowState,
    count: int,
    reference_index: Optional["ReferenceIndex"] = None,
) -> tuple[list[Hypothesis], int]:
    logger.info("Generating %s hypotheses with two-phase tool-based process", count)

    tool_registry = state.get("tool_registry")
    mcp_client = await _get_mcp_client_for_generation(tool_registry)

    _log_warm_start_diagnostics(state.get("articles", []))

    draft_hyps, draft_calls = await _run_draft_phase(
        state, count, mcp_client, tool_registry, reference_index
    )

    hypotheses, validate_calls = await _run_validate_phase(
        state, draft_hyps, mcp_client, tool_registry, reference_index
    )

    _log_generated_hypothesis_methods(hypotheses)

    return hypotheses, draft_calls + validate_calls


__all__ = ["draft_hypotheses", "generate_with_tools", "validate_hypotheses"]
