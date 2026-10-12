import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from co_scientist.core.constants import (
    DEEP_HYPOTHESIS_MAX_TOKENS,
    EXTENDED_MAX_TOKENS,
    GENERATE_LIT_TOOL_MAX_PAPERS,
    HIGH_TEMPERATURE,
    VALIDATION_SYNTHESIS_BATCH_SIZE,
    VALIDATION_SYNTHESIS_MAX_TOKENS_CAP,
    VALIDATION_SYNTHESIS_TOKENS_PER_HYPOTHESIS,
    corpus_slug,
    get_validate_max_iterations,
    scaled_max_tokens,
    strip_citation_markers,
    truncate_for_prompt,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.models import GenerationMethod, Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    ToolLoop,
    call_llm_json,
    call_llm_with_tools,
    parse_tool_loop_json,
)
from co_scientist.platform.retrieval.evidence.search_query import call_search_tool
from co_scientist.platform.retrieval.evidence.search_support import normalize_search_response
from co_scientist.platform.retrieval.tools.provider import MCPToolProvider
from co_scientist.platform.retrieval.tools.response_parser import ResponseParser, parse_mcp_result
from co_scientist.science.citations import (
    hypothesis_from_llm_output,
)
from co_scientist.science.generation.literature_tools.draft import (
    _setup_tool_provider,
)
from co_scientist.science.prompts import (
    ValidationSynthesisRequest,
    get_hypothesis_novelty_analysis_prompt,
    get_validation_synthesis_prompt_with_tools,
)
from co_scientist.science.schemas import HYPOTHESIS_NOVELTY_ANALYSIS_SCHEMA

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolConfig, ToolRegistry


@dataclass(frozen=True)
class _NoveltySearchContext:
    mcp_client: Any
    tool_registry: Optional["ToolRegistry"]
    shared_slug: str
    run_id: str | None


def _find_search_tool(
    tool_registry: Optional["ToolRegistry"],
) -> tuple[str | None, Optional["ToolConfig"]]:
    if not tool_registry:
        return None, None

    tool_ids = tool_registry.get_tools_for_workflow("validation")

    for tool_id in tool_ids:
        tool_config = tool_registry.get_tool(tool_id)
        if tool_config and tool_config.category in (
            "search",
            "search_with_content",
        ):
            return tool_id, tool_config
    return None, None


def _first(*values: Any) -> Any:
    for value in values:
        if value:
            return value
    return values[-1] if values else None


def _articles_to_paper_dict(articles: list[Any]) -> dict[str, dict[str, Any]]:
    papers: dict[str, dict[str, Any]] = {}
    for article in articles:
        paper_id = _first(article.source_id, article.url, article.title)
        papers[paper_id] = {
            "title": article.title,
            "authors": article.authors,
            "year": article.year,
            "fulltext": _first(article.content, article.abstract, ""),
        }
    return papers


def _build_search_canonical_params(
    hypothesis_text: str,
    max_papers: int,
    shared_slug: str,
    run_id: str | None,
) -> dict[str, Any]:
    canonical_params: dict[str, Any] = {
        "query": hypothesis_text[:200],
        "max_papers": max_papers,
        "slug": shared_slug,
    }
    if run_id:
        canonical_params["run_id"] = run_id
    return canonical_params


async def _search_papers_via_tool_config(
    tool_config: "ToolConfig",
    hypothesis_text: str,
    ctx: _NoveltySearchContext,
    max_papers: int,
) -> dict[str, dict[str, Any]]:
    canonical_params = _build_search_canonical_params(
        hypothesis_text, max_papers, ctx.shared_slug, ctx.run_id
    )
    mapped_params = tool_config.map_parameters(canonical_params)

    # The literature-review path's caller, so a tool-reported error raises
    # here instead of parsing as zero prior art, and a transient failure is
    # retried rather than read as an absence of evidence.
    result = await call_search_tool(ctx.mcp_client, tool_config.mcp_tool_name, mapped_params)

    parser = ResponseParser(tool_config)
    articles = parser.parse_to_articles(result)

    return _articles_to_paper_dict(articles)


async def _search_papers_legacy_fallback(
    hypothesis_text: str,
    ctx: _NoveltySearchContext,
    max_papers: int,
) -> dict[str, dict[str, Any]]:
    """No-registry callers retain direct PubMed retrieval; an explicit
    registry with no validation source instead skips search."""
    result = await call_search_tool(
        ctx.mcp_client,
        "pubmed_search_with_fulltext",
        {
            "query": hypothesis_text[:200],
            "max_papers": max_papers,
            "slug": ctx.shared_slug,
            "run_id": ctx.run_id,
        },
    )

    return normalize_search_response(parse_mcp_result(result), None)


def _skip_search_no_tool_configured() -> dict[str, dict[str, Any]]:
    logger.warning("no search tools configured for validation workflow, skipping novelty search")
    return {}


async def _search_papers_for_hypothesis(
    hypothesis_text: str,
    ctx: _NoveltySearchContext,
    max_papers: int,
) -> dict[str, dict[str, Any]]:
    _, tool_config = _find_search_tool(ctx.tool_registry)

    if tool_config:
        return await _search_papers_via_tool_config(tool_config, hypothesis_text, ctx, max_papers)

    if ctx.tool_registry:
        return _skip_search_no_tool_configured()

    return await _search_papers_legacy_fallback(hypothesis_text, ctx, max_papers)


_PaperAnalyzer = Callable[
    [str, int, str, dict[str, Any], str],
    Awaitable[dict[str, Any] | None],
]


async def _gather_novelty_analyses(
    novelty_analysis_tasks: list[Awaitable[dict[str, Any] | None]],
    idx: int,
) -> list[dict[str, Any]]:
    logger.info(
        "Running %s novelty analyses in parallel for hypothesis %s",
        len(novelty_analysis_tasks),
        idx,
    )
    novelty_analyses_results = await asyncio.gather(*novelty_analysis_tasks)

    novelty_analyses = [a for a in novelty_analyses_results if a is not None]
    logger.info(
        "Completed %s novelty analyses for hypothesis %s",
        len(novelty_analyses),
        idx,
    )
    return novelty_analyses


async def _run_parallel_novelty_analyses(
    hypothesis_text: str,
    idx: int,
    papers: dict[str, dict[str, Any]],
    model_name: str,
    analyze_paper: _PaperAnalyzer,
) -> list[dict[str, Any]]:

    novelty_analysis_tasks = [
        analyze_paper(hypothesis_text, idx, paper_id, metadata, model_name)
        for paper_id, metadata in papers.items()
    ]

    if not novelty_analysis_tasks:
        logger.warning("No papers with fulltext found for hypothesis %s", idx)
        return []

    return await _gather_novelty_analyses(novelty_analysis_tasks, idx)


async def _search_papers_for_draft(
    hypothesis_text: str,
    idx: int,
    search_ctx: _NoveltySearchContext,
) -> dict[str, dict[str, Any]] | None:
    try:
        papers = await _search_papers_for_hypothesis(
            hypothesis_text,
            search_ctx,
            max_papers=GENERATE_LIT_TOOL_MAX_PAPERS,
        )
        logger.info("Found %s papers for hypothesis %s", len(papers), idx)
        return papers
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as e:
        logger.error("Failed to search papers for hypothesis %s: %s", idx, e)
        return None


async def _gather_hypothesis_novelty_analyses(
    idx: int,
    total: int,
    draft: dict[str, str],
    model_name: str,
    search_ctx: _NoveltySearchContext,
    analyze_paper: _PaperAnalyzer,
) -> dict[str, Any]:

    hypothesis_text = draft.get("hypothesis") or draft.get("text", "")
    logger.info("Analyzing hypothesis %s/%s (%s characters)", idx, total, len(hypothesis_text))
    papers = await _search_papers_for_draft(hypothesis_text, idx, search_ctx)
    if papers is None:
        _, tool = _find_search_tool(search_ctx.tool_registry)
        source = tool.mcp_tool_name if tool else "pubmed_search_with_fulltext"
        return {
            "draft": draft,
            "novelty_analyses": [],
            "search_status": "unknown",
            "failed_sources": [source],
        }
    novelty_analyses = await _run_parallel_novelty_analyses(
        hypothesis_text, idx, papers, model_name, analyze_paper
    )

    return {"draft": draft, "novelty_analyses": novelty_analyses, "search_status": "ok"}


async def _run_novelty_analysis_stage(
    draft_hypotheses: list[dict[str, str]],
    state: WorkflowState,
    search_ctx: _NoveltySearchContext,
    analyze_paper: _PaperAnalyzer,
) -> list[dict[str, Any]]:
    model_name = state["model_name"]
    total_drafts = len(draft_hypotheses)
    return [
        await _gather_hypothesis_novelty_analyses(
            idx, total_drafts, draft, model_name, search_ctx, analyze_paper
        )
        for idx, draft in enumerate(draft_hypotheses, 1)
    ]


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolRegistry


_SynthesisCaller = Callable[
    [list[dict[str, Any]], str, list[str] | None],
    Awaitable[list[dict[str, Any]]],
]


class _SynthesisContext(NamedTuple):
    state: WorkflowState
    research_goal: str
    max_iterations: int
    tool_registry: Optional["ToolRegistry"]
    reference_index: Any | None
    provider: MCPToolProvider
    openai_tools: list[Any]


class _SynthesisCallInputs(NamedTuple):
    prompt: str
    max_tokens: int


def _setup_validation_tool_provider(
    mcp_client: Any,
    tool_registry: Optional["ToolRegistry"],
    total_hypotheses: int,
) -> tuple[MCPToolProvider, list[Any], Optional["ToolRegistry"], int]:
    provider, openai_tools, tool_registry = _setup_tool_provider(
        mcp_client, tool_registry, "validation", "validation", logger
    )

    # Every batch and individual retry gets the iteration budget sized from the
    # whole hypothesis pool.

    max_iterations = get_validate_max_iterations(total_hypotheses)
    logger.info("Validation synthesis budget: %s iterations", max_iterations)

    return provider, openai_tools, tool_registry, max_iterations


def _compute_synthesis_max_tokens(batch: list[dict[str, Any]], batch_label: str) -> int:
    # Synthesis writes final full-depth hypotheses, so it needs the
    # deep-generation budget.

    synthesis_max_tokens = scaled_max_tokens(
        DEEP_HYPOTHESIS_MAX_TOKENS,
        len(batch),
        per_item=VALIDATION_SYNTHESIS_TOKENS_PER_HYPOTHESIS,
        cap=VALIDATION_SYNTHESIS_MAX_TOKENS_CAP,
    )
    logger.debug(
        "Batch %s token budget: %s for %s hypotheses",
        batch_label,
        synthesis_max_tokens,
        len(batch),
    )
    return synthesis_max_tokens


def _build_synthesis_call_inputs(
    batch: list[dict[str, Any]],
    batch_label: str,
    already_validated_texts: list[str] | None,
    ctx: _SynthesisContext,
) -> _SynthesisCallInputs:
    ref_text = ctx.reference_index.text if ctx.reference_index else ""
    synthesis_prompt, _ = get_validation_synthesis_prompt_with_tools(
        ValidationSynthesisRequest(
            research_goal=ctx.research_goal,
            hypotheses_with_analyses=batch,
            articles=ctx.state.get("articles"),
            articles_with_reasoning=ctx.state.get("articles_with_reasoning"),
            max_iterations=ctx.max_iterations,
            tool_registry=ctx.tool_registry,
            reference_list=ref_text,
            already_validated_texts=already_validated_texts,
        )
    )

    if any(item.get("search_status") == "unknown" for item in batch):
        sources = sorted({s for item in batch for s in item.get("failed_sources", [])})
        synthesis_prompt += (
            "\nPrior-art search failed for "
            + ", ".join(sources)
            + ". Novelty is unknown; award no novelty credit and do not infer originality "
            "from the absence of papers. Do not retry failed sources."
        )
    synthesis_max_tokens = _compute_synthesis_max_tokens(batch, batch_label)

    return _SynthesisCallInputs(synthesis_prompt, synthesis_max_tokens)


def _log_synthesis_tool_call_summary(
    batch_label: str,
    tool_call_counts: dict[str, int],
) -> None:
    total_calls = sum(tool_call_counts.values())
    if total_calls > 0:
        calls_summary = ", ".join(f"{n}={c}" for n, c in tool_call_counts.items())
        logger.info(
            "Batch %s: %s tool calls (%s)",
            batch_label,
            total_calls,
            calls_summary,
        )


def _parse_synthesis_response(final_response: str, batch_label: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = parse_tool_loop_json(
        final_response,
        "hypotheses",
        f"Validation synthesis (batch {batch_label})",
    )
    logger.debug("Batch %s synthesis returned %s hypotheses", batch_label, len(result))
    return result


def _partition_synthesis_results(
    batches: list[list[dict[str, Any]]],
    raw_results: list[list[dict[str, Any]] | BaseException],
) -> tuple[list[dict[str, Any]], list[tuple[int, list[dict[str, Any]]]]]:
    """Parking and spend ceilings are run-wide control flow; treating them as
    batch failures would retry against the same refused cap."""

    for result in raw_results:
        if isinstance(result, TASK_CONTROL_FLOW_ERRORS):
            raise result
    all_validated_hypotheses: list[dict[str, Any]] = []
    failed_batches: list[tuple[int, list[dict[str, Any]]]] = []

    for i, result in enumerate(raw_results):
        if isinstance(result, Exception):
            logger.warning(
                "Batch %s failed (%s); will retry hypotheses individually",
                i + 1,
                result,
            )
            failed_batches.append((i, batches[i]))
        else:
            # mypy cannot narrow the gathered BaseException union across these
            # branches.

            all_validated_hypotheses.extend(result)  # type: ignore[arg-type]

    return all_validated_hypotheses, failed_batches


def _withhold_failed_search_credit(
    batch: list[dict[str, Any]], results: list[dict[str, Any]]
) -> None:
    # Synthesis may refine or reorder drafts within a batch, so an outage
    # prevents credit throughout that batch, without affecting sibling batches.
    sources = sorted({source for item in batch for source in item.get("failed_sources", [])})
    if sources:
        for hypothesis in results:
            hypothesis["novelty_validation"] = {
                "decision": "unknown",
                "novelty_score": 0,
                "failed_sources": sources,
            }


async def _run_synthesis_batches(
    batches: list[list[dict[str, Any]]],
    call_synthesis: _SynthesisCaller,
) -> tuple[list[dict[str, Any]], list[tuple[int, list[dict[str, Any]]]]]:
    """One failed batch must not cancel successful siblings."""

    raw_results = await asyncio.gather(
        *[call_synthesis(batch, str(i + 1), None) for i, batch in enumerate(batches)],
        return_exceptions=True,
    )

    for batch, result in zip(batches, raw_results, strict=True):
        if isinstance(result, list):
            _withhold_failed_search_credit(batch, result)
    all_validated_hypotheses, failed_batches = _partition_synthesis_results(batches, raw_results)
    logger.info(
        "%s/%s batches succeeded, %s need individual retry",
        len(batches) - len(failed_batches),
        len(batches),
        len(failed_batches),
    )

    return all_validated_hypotheses, failed_batches


def _accumulate_retry_result(
    single_result: list[dict[str, Any]],
    all_validated_hypotheses: list[dict[str, Any]],
    accumulated_texts: list[str],
) -> None:
    all_validated_hypotheses.extend(single_result)
    for h in single_result:
        text = h.get("hypothesis", "")
        if text:
            accumulated_texts.append(text)


async def _retry_one_hypothesis(
    batch_idx: int,
    hyp_idx: int,
    hyp_data: dict[str, Any],
    all_validated_hypotheses: list[dict[str, Any]],
    accumulated_texts: list[str],
    call_synthesis: _SynthesisCaller,
) -> None:
    """An individual failed retry drops only that hypothesis; successful
    siblings still continue."""
    label = f"{batch_idx + 1}_retry_{hyp_idx + 1}"
    context = accumulated_texts if accumulated_texts else None
    try:
        single_result = await call_synthesis([hyp_data], label, context)
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as e:
        logger.error(
            "Individual retry failed for batch %s, hypothesis %s: %s",
            batch_idx + 1,
            hyp_idx + 1,
            e,
        )
        return

    _withhold_failed_search_credit([hyp_data], single_result)
    _accumulate_retry_result(
        single_result,
        all_validated_hypotheses,
        accumulated_texts,
    )


async def _retry_failed_synthesis_batches(
    failed_batches: list[tuple[int, list[dict[str, Any]]]],
    all_validated_hypotheses: list[dict[str, Any]],
    call_synthesis: _SynthesisCaller,
) -> None:
    """Individual retries shrink failure scope so one bad hypothesis or
    truncated answer cannot sink its batch-mates."""
    accumulated_texts = [
        h.get("hypothesis", "") for h in all_validated_hypotheses if h.get("hypothesis")
    ]

    for batch_idx, failed_batch in failed_batches:
        for hyp_idx, hyp_data in enumerate(failed_batch):
            await _retry_one_hypothesis(
                batch_idx,
                hyp_idx,
                hyp_data,
                all_validated_hypotheses,
                accumulated_texts,
                call_synthesis,
            )


def _build_hypotheses_from_synthesis(
    all_validated_hypotheses: list[dict[str, Any]],
    reference_index: Any | None,
) -> list[Hypothesis]:
    ref_sources = reference_index.sources if reference_index else {}
    hypotheses = []
    for hyp_data in all_validated_hypotheses:
        hypothesis = hypothesis_from_llm_output(
            hyp_data,
            ref_sources,
            GenerationMethod.LITERATURE_TOOLS,
            novelty_validation=hyp_data.get("novelty_validation"),
        )
        hypotheses.append(hypothesis)
    return hypotheses


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolRegistry


def _build_paper_metadata(paper_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_id": paper_id,
        "title": metadata.get("title", "Unknown"),
        "year": metadata.get("year"),
        "authors": metadata.get("authors", []),
    }


def _synthesis_tool_contract(
    tool_registry: Optional["ToolRegistry"],
) -> dict[str, Any] | None:
    """Tool schemas alone miss endpoint/mapping semantics; include resolved
    configuration."""
    if tool_registry is None:
        return None
    return dataclasses.asdict(tool_registry.config)


def _batch_hypotheses_for_synthesis(
    hypotheses_with_analyses: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    batches = [
        hypotheses_with_analyses[i : i + VALIDATION_SYNTHESIS_BATCH_SIZE]
        for i in range(0, len(hypotheses_with_analyses), VALIDATION_SYNTHESIS_BATCH_SIZE)
    ]
    logger.info(
        "Split into %s batches of up to %s hypotheses",
        len(batches),
        VALIDATION_SYNTHESIS_BATCH_SIZE,
    )
    return batches


async def _run_and_retry_synthesis_batches(
    batches: list[list[dict[str, Any]]],
    call_synthesis: _SynthesisCaller,
) -> list[dict[str, Any]]:
    all_validated_hypotheses, failed_batches = await _run_synthesis_batches(batches, call_synthesis)

    if failed_batches:
        await _retry_failed_synthesis_batches(
            failed_batches, all_validated_hypotheses, call_synthesis
        )

    return all_validated_hypotheses


def _build_synthesis_context(
    hypotheses_with_analyses: list[dict[str, Any]],
    state: WorkflowState,
    mcp_client: Any,
    tool_registry: Optional["ToolRegistry"],
    reference_index: Any | None,
) -> _SynthesisContext:
    logger.info(
        "Running validation synthesis for %s hypotheses in batches of %s",
        len(hypotheses_with_analyses),
        VALIDATION_SYNTHESIS_BATCH_SIZE,
    )
    provider, openai_tools, tool_registry, max_iterations = _setup_validation_tool_provider(
        mcp_client, tool_registry, len(hypotheses_with_analyses)
    )
    return _SynthesisContext(
        state,
        state["research_goal"],
        max_iterations,
        tool_registry,
        reference_index,
        provider,
        openai_tools,
    )


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolRegistry


async def _analyze_paper_novelty(
    hypothesis_text: str,
    hypothesis_idx: int,
    paper_id: str,
    metadata: dict[str, Any],
    model_name: str,
) -> dict[str, Any] | None:
    # Strip a paper's citation markers only from the prompt copy so the verdict cannot echo
    # them as apparent evidence.
    fulltext = strip_citation_markers(truncate_for_prompt(metadata.get("fulltext", "")))
    prompt = get_hypothesis_novelty_analysis_prompt(
        hypothesis_text=hypothesis_text,
        title=metadata.get("title", "Unknown"),
        authors=metadata.get("authors", []),
        year=metadata.get("year"),
        fulltext=fulltext,
    )

    try:
        analysis = await call_llm_json(
            prompt=prompt,
            spec=CompletionSpec(
                role="novelty",
                model_name=model_name,
                max_tokens=EXTENDED_MAX_TOKENS,
                temperature=HIGH_TEMPERATURE,
                json_schema=HYPOTHESIS_NOVELTY_ANALYSIS_SCHEMA,
            ),
        )
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as e:
        logger.error(
            "Failed to analyze paper %s for hypothesis %s: %s",
            paper_id,
            hypothesis_idx,
            e,
        )

        return None

    return {
        "paper_metadata": _build_paper_metadata(paper_id, metadata),
        "analysis": analysis,
    }


async def _invoke_synthesis_llm(
    call_inputs: _SynthesisCallInputs,
    batch_label: str,
    ctx: _SynthesisContext,
) -> tuple[str, dict[str, int]]:
    tracked_executor, tool_call_counts = ctx.provider.tracked_executor(
        f"Validation batch {batch_label}"
    )
    final_response, _ = await call_llm_with_tools(
        prompt=call_inputs.prompt,
        spec=CompletionSpec(
            role="novelty",
            model_name=ctx.state["model_name"],
            max_tokens=call_inputs.max_tokens,
            temperature=HIGH_TEMPERATURE,
        ),
        loop=ToolLoop(
            # Source-derived analyses cannot authorize new outbound actions.
            tools=[],
            executor=tracked_executor,
            max_iterations=ctx.max_iterations,
            tool_contract=_synthesis_tool_contract(ctx.tool_registry),
        ),
        options=LLMCallOptions(
            run_id=ctx.state.get("run_id"),
            prompt_name=f"validation_synthesis_batch_{batch_label}",
        ),
    )
    return final_response, tool_call_counts


async def validate_hypotheses(
    state: WorkflowState,
    draft_hypotheses: list[dict[str, str]],
    mcp_client: Any,
    tool_registry: Optional["ToolRegistry"] = None,
    reference_index: Any | None = None,
) -> tuple[list[Hypothesis], int]:
    logger.info("Phase 2: Validating %s draft hypotheses", len(draft_hypotheses))
    hypotheses_with_analyses = await _run_validate_novelty_stage(
        draft_hypotheses, state, mcp_client, tool_registry
    )
    hypotheses = await _run_synthesis_and_build_hypotheses(
        hypotheses_with_analyses,
        state,
        mcp_client,
        tool_registry,
        reference_index,
    )
    return hypotheses, _count_validation_llm_calls(hypotheses_with_analyses)


async def _run_single_synthesis_call(
    batch: list[dict[str, Any]],
    batch_label: str,
    already_validated_texts: list[str] | None,
    ctx: _SynthesisContext,
) -> list[dict[str, Any]]:
    batch_size = len(batch)
    logger.info("Processing synthesis batch %s (%s hypotheses)", batch_label, batch_size)

    call_inputs = _build_synthesis_call_inputs(batch, batch_label, already_validated_texts, ctx)

    final_response, tool_call_counts = await _invoke_synthesis_llm(
        call_inputs,
        batch_label,
        ctx,
    )
    _log_synthesis_tool_call_summary(batch_label, tool_call_counts)

    return _parse_synthesis_response(final_response, batch_label)


async def _run_synthesis_stage_batches(
    hypotheses_with_analyses: list[dict[str, Any]],
    ctx: _SynthesisContext,
) -> list[dict[str, Any]]:
    batches = _batch_hypotheses_for_synthesis(hypotheses_with_analyses)

    async def _call_synthesis(
        batch: list[dict[str, Any]],
        batch_label: str,
        already_validated_texts: list[str] | None,
    ) -> list[dict[str, Any]]:
        return await _run_single_synthesis_call(batch, batch_label, already_validated_texts, ctx)

    all_validated_hypotheses = await _run_and_retry_synthesis_batches(batches, _call_synthesis)
    logger.info(
        "Combined %s validated hypotheses from %s batches",
        len(all_validated_hypotheses),
        len(batches),
    )
    return all_validated_hypotheses


async def _run_validate_novelty_stage(
    draft_hypotheses: list[dict[str, str]],
    state: WorkflowState,
    mcp_client: Any,
    tool_registry: Optional["ToolRegistry"],
) -> list[dict[str, Any]]:
    # Reuse the corpus slug from drafting for warm retrieval.
    shared_slug = corpus_slug(state["research_goal"])
    logger.info("Reusing shared corpus from draft phase: %s", shared_slug)

    search_ctx = _NoveltySearchContext(
        mcp_client=mcp_client,
        tool_registry=tool_registry,
        shared_slug=shared_slug,
        run_id=state.get("run_id"),
    )

    return await _run_novelty_analysis_stage(
        draft_hypotheses,
        state,
        search_ctx,
        analyze_paper=_analyze_paper_novelty,
    )


async def _run_synthesis_and_build_hypotheses(
    hypotheses_with_analyses: list[dict[str, Any]],
    state: WorkflowState,
    mcp_client: Any,
    tool_registry: Optional["ToolRegistry"],
    reference_index: Any | None,
) -> list[Hypothesis]:
    ctx = _build_synthesis_context(
        hypotheses_with_analyses,
        state,
        mcp_client,
        tool_registry,
        reference_index,
    )
    all_validated_hypotheses = await _run_synthesis_stage_batches(hypotheses_with_analyses, ctx)

    hypotheses = _build_hypotheses_from_synthesis(all_validated_hypotheses, reference_index)
    logger.info("Generated %s validated hypotheses", len(hypotheses))
    return hypotheses


def _count_validation_llm_calls(
    hypotheses_with_analyses: list[dict[str, Any]],
) -> int:
    """Legacy node metrics are a floor; failed calls, retries and tool turns
    remain counted by transport telemetry."""
    novelty_calls = sum(
        len(item.get("novelty_analyses") or []) for item in hypotheses_with_analyses
    )
    synthesis_calls = len(_batch_hypotheses_for_synthesis(hypotheses_with_analyses))
    return novelty_calls + synthesis_calls
