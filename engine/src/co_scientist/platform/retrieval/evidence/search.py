import asyncio
import logging
from typing import TYPE_CHECKING, Any

from co_scientist.core.constants import SOURCE_FANOUT, corpus_slug, distinct_queries
from co_scientist.platform.retrieval.evidence.relevance import (
    apply_semantic_relevance,
)
from co_scientist.platform.retrieval.evidence.search_fusion import (
    select_within_budget,
)
from co_scientist.platform.retrieval.evidence.search_query import (
    _search_single_query as _search_single_query,
)
from co_scientist.platform.retrieval.evidence.search_query import (
    _search_source_for_query as _search_source_for_query,
)
from co_scientist.platform.retrieval.evidence.search_query import (
    _SearchRunContext as _SearchRunContext,
)
from co_scientist.platform.retrieval.evidence.search_support import (
    SearchConfig,
    SearchState,
    extract_source_name,
    merge_search_results,
)
from co_scientist.platform.retrieval.mcp_client import MCPToolClient


async def collect_papers(
    queries: list[str],
    state: SearchState,
    config: SearchConfig,
    mcp_client: MCPToolClient,
    search_errors: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The corpus slug permits reuse across runs and tool-based generation
    searches."""
    queries = distinct_queries(queries)
    ctx = _SearchRunContext(
        slug=corpus_slug(state["research_goal"]),
        run_id=state["run_id"],
        mcp_client=mcp_client,
        errors=search_errors,
    )

    if config.is_multi_source:
        return await _phase2_collect_papers_multi_source(queries, config, ctx)
    return await _phase2_collect_papers_single_source(queries, config, ctx)


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import SearchSourceConfig, ToolConfig, ToolRegistry

logger = logging.getLogger(__name__)


async def _run_single_source_queries(
    queries: list[str],
    ctx: _SearchRunContext,
    tool_config: "ToolConfig",
    src_name: str,
    papers_per_query: int,
) -> dict[str, dict[str, Any]]:
    """Merge concurrent queries in input order: later queries win duplicate
    IDs, so timing cannot change winning metadata."""
    per_query = await asyncio.gather(
        *(
            _search_source_for_query(query, ctx, tool_config, src_name, papers_per_query)
            for query in queries
        )
    )
    source_results: dict[str, dict[str, Any]] = {}
    for normalized in per_query:
        source_results.update(normalized)
    return source_results


async def _search_single_source(
    source_config: "SearchSourceConfig",
    queries: list[str],
    ctx: _SearchRunContext,
    tool_registry: "ToolRegistry",
) -> tuple[str, dict[str, dict[str, Any]]]:
    tool_config = tool_registry.get_tool(source_config.tool)
    if not tool_config:
        logger.warning("Tool config not found for source: %s", source_config.tool)
        return (source_config.tool, {})

    src_name = extract_source_name(tool_config)
    papers_per_query = source_config.papers_per_query
    logger.info(
        "Searching %s (%s): %s papers/query",
        src_name,
        tool_config.mcp_tool_name,
        papers_per_query,
    )

    source_results = await _run_single_source_queries(
        queries, ctx, tool_config, src_name, papers_per_query
    )

    logger.info("Source %s: collected %s papers", src_name, len(source_results))
    return (source_config.tool, source_results)


async def _search_all_sources(
    enabled_sources: list["SearchSourceConfig"],
    queries: list[str],
    ctx: _SearchRunContext,
    tool_registry: "ToolRegistry",
) -> list[tuple[str, dict[str, dict[str, Any]]]]:
    # Created per call: a semaphore belongs to the running loop, and worker
    # cohorts run separate loops.
    fanout = asyncio.Semaphore(SOURCE_FANOUT)

    async def bounded(source: "SearchSourceConfig") -> tuple[str, dict[str, dict[str, Any]]]:
        async with fanout:
            return await _search_single_source(source, queries, ctx, tool_registry)

    return await asyncio.gather(*(bounded(source) for source in enabled_sources))


async def _apply_semantic_relevance_if_enabled(
    ranked: dict[str, dict[str, Any]],
    config: SearchConfig,
) -> dict[str, dict[str, Any]]:
    """Probes skip run-level semantic batching to avoid multiplied cost;
    skipping changes ranking, not admission count."""
    if not config.semantic_relevance_enabled:
        return ranked
    return await apply_semantic_relevance(
        ranked,
        config.research_goal,
        config.model_name,
        config.papers_to_read_count,
    )


async def _merge_and_budget_multi_source(
    source_results: list[tuple[str, dict[str, dict[str, Any]]]],
    enabled_sources: list["SearchSourceConfig"],
    config: SearchConfig,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    assert config.workflow is not None
    all_paper_metadata, paper_source_map = merge_search_results(
        source_results,
        deduplicate=config.workflow.deduplicate_across_sources,
    )
    all_paper_metadata = await _apply_semantic_relevance_if_enabled(all_paper_metadata, config)
    selected_ids = select_within_budget(
        all_paper_metadata,
        paper_source_map,
        enabled_sources,
        config.papers_to_read_count,
    )
    all_paper_metadata = {paper_id: all_paper_metadata[paper_id] for paper_id in selected_ids}
    paper_source_map = {
        paper_id: paper_source_map[paper_id]
        for paper_id in selected_ids
        if paper_id in paper_source_map
    }
    return all_paper_metadata, paper_source_map


async def _phase2_collect_papers_multi_source(
    queries: list[str],
    config: SearchConfig,
    ctx: _SearchRunContext,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:

    assert config.workflow is not None and config.tool_registry is not None
    # Registry loading already reconciles source flags with disabled tools.
    enabled_sources = config.workflow.get_enabled_search_sources()
    logger.info("Phase 2: collecting papers from %s sources", len(enabled_sources))

    source_results = await _search_all_sources(enabled_sources, queries, ctx, config.tool_registry)

    all_paper_metadata, paper_source_map = await _merge_and_budget_multi_source(
        source_results, enabled_sources, config
    )

    logger.info(
        "Multi-source search complete: %s unique papers from %s sources",
        len(all_paper_metadata),
        len(enabled_sources),
    )

    return all_paper_metadata, paper_source_map


async def _search_all_queries(
    queries: list[str],
    papers_per_query: int,
    ctx: _SearchRunContext,
    config: SearchConfig,
) -> list[dict[str, dict[str, Any]]]:
    tasks = [
        _search_single_query(query, i + 1, papers_per_query, ctx, config)
        for i, query in enumerate(queries)
    ]
    return await asyncio.gather(*tasks)


async def _combine_and_cap_single_source_results(
    search_results: list[dict[str, dict[str, Any]]],
    config: SearchConfig,
) -> dict[str, dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    for result_data in search_results:
        combined.update(result_data)
    ranked, _ = merge_search_results([(config.search_tool_name, combined)], deduplicate=True)
    ranked = await _apply_semantic_relevance_if_enabled(ranked, config)
    return dict(list(ranked.items())[: config.papers_to_read_count])


async def _phase2_collect_papers_single_source(
    queries: list[str],
    config: SearchConfig,
    ctx: _SearchRunContext,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    logger.info("Phase 2: collecting papers with %s", config.search_tool_name)

    logger.info(
        "Over-fetching %s papers per query for a %s-paper unique corpus",
        config.papers_to_read_count,
        config.papers_to_read_count,
    )

    # Expanded queries overlap: overfetch each, then count unique sources
    # globally.
    search_results = await _search_all_queries(queries, config.papers_to_read_count, ctx, config)

    all_paper_metadata = await _combine_and_cap_single_source_results(search_results, config)
    return all_paper_metadata, {}
