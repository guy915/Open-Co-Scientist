import logging
from typing import Any

from co_scientist.core.constants import LITERATURE_REVIEW_FAILED
from co_scientist.domains.research_state.models import MetricDeltas, create_metrics_update
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.degradation import (
    resolve_retrieval_degradation,
)
from co_scientist.platform.retrieval.evidence.article_support import (
    make_failure_result,
    make_success_result,
)
from co_scientist.platform.retrieval.evidence.search_support import (
    SearchConfig,
)
from co_scientist.platform.retrieval.evidence.search_support import (
    search_config_for as search_config_for,
)
from co_scientist.platform.retrieval.mcp_client import (
    MCPToolClient,
    check_mcp_available,
    get_mcp_client,
)
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.generation.literature_review.orchestration import (
    _analyze_and_synthesize as _analyze_and_synthesize,
)
from co_scientist.science.generation.literature_review.orchestration import (
    _collect_and_enrich_papers as _collect_and_enrich_papers,
)
from co_scientist.science.generation.literature_review.orchestration import (
    _CollectionResult as _CollectionResult,
)
from co_scientist.science.generation.literature_review.orchestration import (
    _emit_and_log_completion as _emit_and_log_completion,
)
from co_scientist.science.generation.literature_review.orchestration import (
    _finalize_synthesis_and_articles as _finalize_synthesis_and_articles,
)
from co_scientist.science.generation.literature_review.orchestration import (
    _handle_collection_edge_cases as _handle_collection_edge_cases,
)
from co_scientist.science.generation.literature_review.orchestration import (
    _log_sample_papers as _log_sample_papers,
)
from co_scientist.science.generation.literature_review.orchestration import (
    _ReviewSynthesis as _ReviewSynthesis,
)
from co_scientist.science.generation.literature_review.queries import (
    QueryPhaseResult,
    _phase1_generate_queries,
)
from co_scientist.science.generation.literature_review.research_phase import (
    ResearchOutcome,
    run_research_phase,
)

logger = logging.getLogger(__name__)


def _initialize_review(state: WorkflowState) -> SearchConfig:
    config = search_config_for(state)
    logger.info(
        "Literature review config: dev_mode=%s, papers=%s",
        config.is_dev_mode,
        config.papers_to_read_count,
    )
    return config


async def _check_server_available(
    state: WorkflowState,
    config: SearchConfig,
) -> dict[str, Any] | None:
    """Only an unreachable MCP server vetoes review; a single unavailable
    source must not block reachable siblings."""
    server_available = await check_mcp_available(tool_registry=config.tool_registry)
    if server_available:
        return None

    logger.error("Literature review MCP server unavailable")
    await emit_progress(
        state,
        "literature_review_error",
        "Literature review failed (MCP server unavailable)",
        0.2,
    )
    result = make_failure_result("literature source service unavailable")
    # Losing MCP mid-node must carry the same run-wide degradation as losing it
    # at setup.

    result["retrieval_degradation"] = resolve_retrieval_degradation(
        mcp_available=False,
        private_sources=state.get("context_enrichment_sources"),
    )
    return result


def _with_llm_call_metrics(result: dict[str, Any], llm_calls: int) -> dict[str, Any]:
    """Every exit must retain real node spend so early failures cannot bypass
    max_llm_calls."""
    result["metrics"] = create_metrics_update(deltas=MetricDeltas(llm_calls=llm_calls))
    return result


async def _prepare_review(
    state: WorkflowState,
) -> tuple[SearchConfig, MCPToolClient] | dict[str, Any]:
    config = _initialize_review(state)

    unavailable_result = await _check_server_available(state, config)
    if unavailable_result is not None:
        return unavailable_result

    await emit_progress(state, "literature_review_start", "Conducting literature review...", 0.1)

    mcp_client = await get_mcp_client(tool_registry=config.tool_registry)
    return config, mcp_client


def _merge_research(
    reviewed: _ReviewSynthesis,
    collected: _CollectionResult,
    research: ResearchOutcome | None,
) -> str:
    """Keep analyzed records over fresh research hits. Preserve the exact
    failure sentinel even when research finds new evidence."""
    if research is None:
        return reviewed.text
    pool = collected.all_paper_metadata
    for locator, record in research.records.items():
        if locator not in pool:
            pool[locator] = record
    if reviewed.text == LITERATURE_REVIEW_FAILED:
        return reviewed.text
    return reviewed.text + research.section


async def _finalize_review(
    state: WorkflowState,
    config: SearchConfig,
    collected: _CollectionResult,
    query_result: QueryPhaseResult,
    reviewed: _ReviewSynthesis,
    research: ResearchOutcome | None,
) -> dict[str, Any]:
    queries = query_result.queries
    synthesis, articles = _finalize_synthesis_and_articles(
        _merge_research(reviewed, collected, research),
        collected.all_paper_metadata,
        collected.context_enrichment_sources,
        config.source_name,
    )

    await _emit_and_log_completion(state, queries, articles, collected.search_errors, synthesis)

    result = make_success_result(synthesis, queries, articles)
    if collected.context_enrichment_sources:
        result["context_enrichment_sources"] = collected.context_enrichment_sources
    if research is not None:
        result["research_ledgers"] = [research.ledger]
    return _with_llm_call_metrics(result, query_result.llm_calls + reviewed.llm_calls)


async def _run_search_phases(
    state: WorkflowState,
    config: SearchConfig,
    mcp_client: MCPToolClient,
) -> tuple[QueryPhaseResult, _CollectionResult] | dict[str, Any]:
    query_result = await _phase1_generate_queries(state, config, mcp_client)
    queries = query_result.queries

    collected = await _collect_and_enrich_papers(queries, state, config, mcp_client)

    edge_case_result = await _handle_collection_edge_cases(state, collected, queries, config)
    if edge_case_result is not None:
        return _with_llm_call_metrics(edge_case_result, query_result.llm_calls)

    _log_sample_papers(collected.all_paper_metadata)
    return query_result, collected


async def literature_review_node(state: WorkflowState) -> dict[str, Any]:
    logger.info("Starting literature review node")

    prepared = await _prepare_review(state)
    if isinstance(prepared, dict):
        return prepared
    config, mcp_client = prepared

    phase_result = await _run_search_phases(state, config, mcp_client)
    if isinstance(phase_result, dict):
        return phase_result
    query_result, collected = phase_result

    reviewed = await _analyze_and_synthesize(
        collected.all_paper_metadata, state, collected.background_context
    )
    research = await run_research_phase(state, config, mcp_client, reviewed.analyses)

    return await _finalize_review(state, config, collected, query_result, reviewed, research)
