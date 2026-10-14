import asyncio
import dataclasses
import logging
from typing import Any, NamedTuple, cast

from co_scientist.core.constants import (
    LITERATURE_REVIEW_FAILED,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.article import Article
from co_scientist.platform.retrieval.evidence.article_support import (
    _has_fulltext,
    build_articles_from_metadata,
    count_papers_with_fulltext,
    get_papers_with_content,
    make_failure_result,
)
from co_scientist.platform.retrieval.evidence.retrieval_support import (
    ContentToolConfig,
    build_content_config,
    build_pdf_discovery_config,
    get_papers_needing_content,
    get_papers_needing_pdf_discovery,
    parse_content_result,
    parse_pdf_discovery_result,
)
from co_scientist.platform.retrieval.evidence.search_support import (
    SearchConfig,
)
from co_scientist.platform.retrieval.mcp_client import MCPToolClient
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.generation.literature_review.enrichment import (
    _format_kg_section_with_keys,
    _phase2_6_fetch_context_enrichment,
)
from co_scientist.science.generation.literature_review.synthesis import (
    _phase3_analyze_papers,
    _phase4_synthesize,
)

logger = logging.getLogger(__name__)


async def _emit_empty_search_diagnostics(
    state: WorkflowState,
    queries: list[str],
    search_errors: list[str],
) -> None:
    """Broken searches and genuine zero hits both yield no papers;
    diagnostics must distinguish them."""
    if search_errors:
        logger.error(
            "Literature review found no papers: %s of %s search call(s) errored: %s",
            len(search_errors),
            len(queries),
            "; ".join(search_errors[:5]),
        )
        await emit_progress(
            state,
            "literature_review_error",
            "Literature search failed (no papers retrieved)",
            0.2,
            queries_count=len(queries),
            search_errors_count=len(search_errors),
            search_error_sample=search_errors[:5],
        )
    else:
        logger.warning(
            "Literature review found no papers: all %s query/queries returned zero results"
            " (no errors)",
            len(queries),
        )
        await emit_progress(
            state,
            "literature_review_empty",
            "Literature search returned no results",
            0.2,
            queries_count=len(queries),
            search_errors_count=0,
        )


async def _handle_no_papers_found(
    state: WorkflowState,
    queries: list[str],
) -> dict[str, Any]:
    logger.warning("No papers collected")
    await emit_progress(
        state,
        "literature_review_complete",
        "Literature review completed (no papers found)",
        0.2,
    )
    return make_failure_result("no papers found", queries=queries)


async def _handle_no_fulltext_available(
    state: WorkflowState,
    all_paper_metadata: dict[str, dict[str, Any]],
    queries: list[str],
    source_name: str,
) -> dict[str, Any]:
    """Metadata-only papers remain visible but unused; they cannot count as
    analyzed evidence."""
    logger.error("No papers have fulltext or abstracts available - cannot analyze")
    n = len(all_paper_metadata)
    await emit_progress(
        state,
        "literature_review_complete",
        f"Literature review failed ({n} papers found but none have fulltexts)",
        0.2,
    )
    articles = build_articles_from_metadata(all_paper_metadata, source_name)
    return make_failure_result(
        f"{n} papers found but none have fulltexts for analysis",
        queries=queries,
        articles=articles,
    )


def _log_sample_papers(all_paper_metadata: dict[str, dict[str, Any]]) -> None:
    for paper_id, meta in list(all_paper_metadata.items())[:3]:
        has_ft = _has_fulltext(meta)
        logger.debug(
            "Paper %s: title='%s...' has_fulltext=%s",
            paper_id,
            meta.get("title", "")[:60],
            has_ft,
        )


@dataclasses.dataclass
class _CollectionResult:
    all_paper_metadata: dict[str, dict[str, Any]]
    paper_source_map: dict[str, str]
    search_errors: list[str]
    background_context: str
    context_enrichment_sources: list[dict[str, Any]]


def _merge_private_sources(
    state: WorkflowState,
    background_context: str,
    context_enrichment_sources: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    private_sources = state.get("context_enrichment_sources") or []
    if not private_sources:
        return background_context, context_enrichment_sources

    context_enrichment_sources = [
        *private_sources,
        *context_enrichment_sources,
    ]
    private_context = "\n\n".join(str(item.get("display") or "") for item in private_sources)
    background_context = "\n\n".join(part for part in (private_context, background_context) if part)
    return background_context, context_enrichment_sources


def _log_collection_summary(
    all_paper_metadata: dict[str, dict[str, Any]],
) -> None:
    with_fulltext, without_fulltext = count_papers_with_fulltext(all_paper_metadata)
    # Abstract-only retrieval is routine for paywalled literature, not an
    # exceptional warning.

    logger.info(
        "Collected %s papers (%s with fulltext, %s abstract only)",
        len(all_paper_metadata),
        with_fulltext,
        without_fulltext,
    )


async def _collect_papers_with_diagnostics(
    queries: list[str],
    state: WorkflowState,
    config: SearchConfig,
    mcp_client: MCPToolClient,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    # Resolve at call time so installed operation patches reach this consumer.
    from co_scientist.platform.retrieval.evidence.search import (
        collect_papers,
    )

    search_errors: list[str] = []
    all_paper_metadata, paper_source_map = await collect_papers(
        queries, state, config, mcp_client, search_errors
    )
    if not all_paper_metadata:
        await _emit_empty_search_diagnostics(state, queries, search_errors)
    return all_paper_metadata, paper_source_map, search_errors


async def _enrich_collected_papers(
    all_paper_metadata: dict[str, dict[str, Any]],
    paper_source_map: dict[str, str],
    config: SearchConfig,
    mcp_client: MCPToolClient,
    state: WorkflowState,
) -> tuple[str, list[dict[str, Any]]]:
    (
        background_context,
        context_enrichment_sources,
    ) = await _fetch_content_and_enrichment(
        all_paper_metadata, paper_source_map, config, mcp_client, state
    )
    return _merge_private_sources(state, background_context, context_enrichment_sources)


async def _discover_pdf_link(
    paper_id: str,
    metadata: dict[str, Any],
    tool_name: str,
    url_field: str,
    mcp_client: MCPToolClient,
) -> tuple[str, str | None]:
    landing_url = metadata.get(url_field)
    if not landing_url:
        return (paper_id, None)

    try:
        logger.debug("Discovering PDF links for %s: %s", paper_id, landing_url)
        result = await mcp_client.call_tool(tool_name, url=landing_url)
        pdf_url = parse_pdf_discovery_result(result)
        if pdf_url:
            logger.debug("Found PDF link for %s: %s", paper_id, pdf_url)
        return (paper_id, pdf_url)
    except Exception as e:
        # Missing a PDF does not prevent abstract-only analysis.

        logger.warning("Failed to discover PDF links for %s: %s", paper_id, e)
        return (paper_id, None)


def _apply_metadata_field(
    all_paper_metadata: dict[str, dict[str, Any]],
    results: list[tuple[str, str | None]],
    field: str,
) -> int:
    updated_count = 0
    for paper_id, value in results:
        if value and paper_id in all_paper_metadata:
            all_paper_metadata[paper_id][field] = value
            updated_count += 1
    return updated_count


async def _run_pdf_discovery(
    papers_needing_discovery: list[tuple[str, dict[str, Any], str, str]],
    mcp_client: MCPToolClient,
    all_paper_metadata: dict[str, dict[str, Any]],
) -> int:
    tasks = [
        _discover_pdf_link(pid, meta, tool_name, url_field, mcp_client)
        for pid, meta, tool_name, url_field in papers_needing_discovery
    ]
    results = await asyncio.gather(*tasks)
    return _apply_metadata_field(all_paper_metadata, results, "pdf_url")


async def _run_and_log_pdf_discovery(
    papers_needing_discovery: list[tuple[str, dict[str, Any], str, str]],
    mcp_client: MCPToolClient,
    all_paper_metadata: dict[str, dict[str, Any]],
) -> None:
    logger.info(
        "Phase 2.4: discovering PDF links for %s papers",
        len(papers_needing_discovery),
    )

    discovered_count = await _run_pdf_discovery(
        papers_needing_discovery, mcp_client, all_paper_metadata
    )

    logger.info(
        "PDF discovery complete: %s/%s papers",
        discovered_count,
        len(papers_needing_discovery),
    )


async def _phase2_4_discover_pdf_links(
    all_paper_metadata: dict[str, dict[str, Any]],
    paper_source_map: dict[str, str],
    config: SearchConfig,
    mcp_client: MCPToolClient,
) -> None:

    pdf_discovery_config = build_pdf_discovery_config(
        config.workflow,
        config.tool_registry,
        config.is_multi_source,
    )

    if not pdf_discovery_config:
        return

    papers_needing_discovery = get_papers_needing_pdf_discovery(
        all_paper_metadata,
        paper_source_map,
        pdf_discovery_config,
    )

    if not papers_needing_discovery:
        return

    await _run_and_log_pdf_discovery(papers_needing_discovery, mcp_client, all_paper_metadata)


def _prepare_content_call_args(
    paper_id: str,
    content_url: str,
    content_cfg: "ContentToolConfig",
    runtime_context: dict[str, Any],
) -> dict[str, Any]:
    # Local import avoids the config.schema/nodes package cycle.

    from co_scientist.platform.retrieval.config.schema import (
        resolve_content_params,
    )

    resolved_params = resolve_content_params(content_cfg.content_params, runtime_context)
    tool_args = {"url": content_url, **resolved_params}

    logger.debug(
        "Fetching content for %s via %s: %s",
        paper_id,
        content_cfg.mcp_tool_name,
        content_url,
    )
    if resolved_params:
        logger.debug("  with params: %s", list(resolved_params.keys()))

    return tool_args


async def _fetch_paper_content(
    paper_id: str,
    metadata: dict[str, Any],
    content_cfg: "ContentToolConfig",
    mcp_client: MCPToolClient,
    runtime_context: dict[str, Any],
) -> tuple[str, str | None]:
    content_url = metadata.get(content_cfg.url_field)
    if not content_url:
        return (paper_id, None)

    try:
        tool_args = _prepare_content_call_args(paper_id, content_url, content_cfg, runtime_context)
        result = await mcp_client.call_tool(content_cfg.mcp_tool_name, **tool_args)
        content = parse_content_result(result)
        if content:
            logger.debug("Retrieved %s chars for paper %s", len(content), paper_id)
        return (paper_id, content)
    except Exception as e:
        # Missing fulltext still permits abstract-only analysis.

        logger.warning("Failed to fetch content for %s: %s", paper_id, e)
        return (paper_id, None)


def _build_content_runtime_context(state: "WorkflowState") -> dict[str, Any]:
    return {
        "research_goal": state.get("research_goal", ""),
        "focus_areas": [],
    }


async def _run_content_fetch(
    papers_needing_content: list[tuple[str, dict[str, Any], "ContentToolConfig"]],
    mcp_client: MCPToolClient,
    runtime_context: dict[str, Any],
    all_paper_metadata: dict[str, dict[str, Any]],
) -> int:
    tasks = [
        _fetch_paper_content(pid, meta, content_cfg, mcp_client, runtime_context)
        for pid, meta, content_cfg in papers_needing_content
    ]
    results = await asyncio.gather(*tasks)
    return _apply_metadata_field(all_paper_metadata, results, "fulltext")


async def _run_and_log_content_fetch(
    papers_needing_content: list[tuple[str, dict[str, Any], "ContentToolConfig"]],
    mcp_client: MCPToolClient,
    runtime_context: dict[str, Any],
    all_paper_metadata: dict[str, dict[str, Any]],
) -> None:
    logger.info("Phase 2.5: fetching content for %s papers", len(papers_needing_content))

    fetched_count = await _run_content_fetch(
        papers_needing_content, mcp_client, runtime_context, all_paper_metadata
    )

    logger.info(
        "Content retrieval complete: %s/%s papers",
        fetched_count,
        len(papers_needing_content),
    )


async def _phase2_5_fetch_content(
    all_paper_metadata: dict[str, dict[str, Any]],
    paper_source_map: dict[str, str],
    config: SearchConfig,
    mcp_client: MCPToolClient,
    state: "WorkflowState",
) -> None:

    content_config = build_content_config(
        config.workflow,
        config.tool_registry,
        config.is_multi_source,
    )

    if not content_config:
        return

    logger.info("Content retrieval configured for %s source(s)", len(content_config))

    papers_needing_content = get_papers_needing_content(
        all_paper_metadata,
        paper_source_map,
        content_config,
    )

    if not papers_needing_content:
        return

    runtime_context = _build_content_runtime_context(state)
    await _run_and_log_content_fetch(
        papers_needing_content, mcp_client, runtime_context, all_paper_metadata
    )


def _count_used_papers(articles: list[Article]) -> int:
    """Count analyzed papers exactly as generation does so enrichment
    citations continue its namespace."""
    return sum(1 for a in articles if getattr(a, "used_in_analysis", False))


def _append_kg_evidence_section(
    synthesis: str,
    articles: list[Article],
    context_enrichment_sources: list[dict[str, Any]],
) -> str:
    if not context_enrichment_sources or synthesis == LITERATURE_REVIEW_FAILED:
        return synthesis

    used_paper_count = _count_used_papers(articles)
    kg_section = _format_kg_section_with_keys(context_enrichment_sources, used_paper_count)
    if not kg_section:
        return synthesis

    logger.info(
        "Appended %s KG source(s) with [C%s...] keys to synthesis",
        len(context_enrichment_sources),
        used_paper_count + 1,
    )
    return synthesis + kg_section


async def _discover_then_fetch_content(
    all_paper_metadata: dict[str, dict[str, Any]],
    paper_source_map: dict[str, str],
    config: SearchConfig,
    mcp_client: MCPToolClient,
    state: WorkflowState,
) -> None:
    """Content fetching depends on PDF URLs produced by discovery, so these
    steps must remain sequential."""
    await _phase2_4_discover_pdf_links(all_paper_metadata, paper_source_map, config, mcp_client)
    await _phase2_5_fetch_content(all_paper_metadata, paper_source_map, config, mcp_client, state)


async def _fetch_content_and_enrichment(
    all_paper_metadata: dict[str, dict[str, Any]],
    paper_source_map: dict[str, str],
    config: SearchConfig,
    mcp_client: MCPToolClient,
    state: WorkflowState,
) -> tuple[str, list[dict[str, Any]]]:
    """Goal-based enrichment is independent of retrieval; overlap them but
    isolate faults to preserve sibling work."""
    retrieval_task = _discover_then_fetch_content(
        all_paper_metadata, paper_source_map, config, mcp_client, state
    )
    enrichment_task = _phase2_6_fetch_context_enrichment(state, config, mcp_client)
    retrieval_result, enrichment_result = await asyncio.gather(
        retrieval_task, enrichment_task, return_exceptions=True
    )
    if isinstance(retrieval_result, BaseException):
        logger.error("Paper retrieval failed: %s", retrieval_result)
    if isinstance(enrichment_result, BaseException):
        logger.error("Context enrichment failed: %s", enrichment_result)
        return "", []
    return cast(tuple[str, list[dict[str, Any]]], enrichment_result)


async def _collect_and_enrich_papers(
    queries: list[str],
    state: WorkflowState,
    config: SearchConfig,
    mcp_client: MCPToolClient,
) -> _CollectionResult:
    (
        all_paper_metadata,
        paper_source_map,
        search_errors,
    ) = await _collect_papers_with_diagnostics(queries, state, config, mcp_client)

    (
        background_context,
        context_enrichment_sources,
    ) = await _enrich_collected_papers(
        all_paper_metadata, paper_source_map, config, mcp_client, state
    )

    _log_collection_summary(all_paper_metadata)

    return _CollectionResult(
        all_paper_metadata=all_paper_metadata,
        paper_source_map=paper_source_map,
        search_errors=search_errors,
        background_context=background_context,
        context_enrichment_sources=context_enrichment_sources,
    )


async def _handle_collection_edge_cases(
    state: WorkflowState,
    collected: _CollectionResult,
    queries: list[str],
    config: SearchConfig,
) -> dict[str, Any] | None:
    if len(collected.all_paper_metadata) == 0:
        return await _handle_no_papers_found(state, queries)

    if not get_papers_with_content(collected.all_paper_metadata):
        return await _handle_no_fulltext_available(
            state, collected.all_paper_metadata, queries, config.source_name
        )

    return None


class _ReviewSynthesis(NamedTuple):
    """Carry analyses to seed follow-up research from paper-identified gaps;
    those gaps are recorded nowhere else."""

    text: str
    llm_calls: int
    analyses: list[dict[str, Any]]


async def _analyze_and_synthesize(
    all_paper_metadata: dict[str, dict[str, Any]],
    state: WorkflowState,
    background_context: str,
) -> _ReviewSynthesis:
    """The legacy node metric is a call-count floor; failed analyses may
    spend calls that transport telemetry counts separately."""
    paper_analyses = await _phase3_analyze_papers(all_paper_metadata, state)
    if not paper_analyses:
        return _ReviewSynthesis(LITERATURE_REVIEW_FAILED, 0, [])
    synthesis = await _phase4_synthesize(paper_analyses, state, background_context)
    return _ReviewSynthesis(synthesis, len(paper_analyses) + 1, paper_analyses)


def _finalize_synthesis_and_articles(
    synthesis: str,
    all_paper_metadata: dict[str, dict[str, Any]],
    context_enrichment_sources: list[dict[str, Any]],
    source_name: str,
) -> tuple[str, list[Article]]:
    logger.info("Phase 5: creating article objects")
    articles = build_articles_from_metadata(all_paper_metadata, source_name)
    logger.info("Created %s article objects", len(articles))

    synthesis = _append_kg_evidence_section(synthesis, articles, context_enrichment_sources)
    return synthesis, articles


async def _emit_and_log_completion(
    state: WorkflowState,
    queries: list[str],
    articles: list[Article],
    search_errors: list[str],
    synthesis: str,
) -> None:
    await emit_progress(
        state,
        "literature_review_complete",
        "Literature review completed",
        0.2,
        queries_count=len(queries),
        articles_count=len(articles),
        articles_analyzed=_count_used_papers(articles),
        fulltext_analyzed=sum(
            bool(article.used_in_analysis and article.content) for article in articles
        ),
        abstract_only_analyzed=sum(
            bool(article.used_in_analysis and not article.content and article.abstract)
            for article in articles
        ),
        search_errors_count=len(search_errors),
    )

    logger.info(
        "Literature review complete: %s articles from %s queries, %s char synthesis",
        len(articles),
        len(queries),
        len(synthesis),
    )
