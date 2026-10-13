import asyncio
import logging
from collections import Counter
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from co_scientist.core.constants import (
    LITERATURE_REVIEW_FAILED,
    MAX_CONCURRENT_LLM_CALLS,
    MAX_PLANNING_IDEAS,
    PROGRESS_GENERATE_COMPLETE,
    PROGRESS_GENERATE_START,
)
from co_scientist.core.exceptions import GenerationError
from co_scientist.domains.research_state.models import GenerationMethod, Hypothesis
from co_scientist.domains.research_state.state import AppendHypotheses, WorkflowState
from co_scientist.platform.retrieval.config.schema import EnrichmentConfig, ToolConfig
from co_scientist.platform.retrieval.mcp_client import get_mcp_client
from co_scientist.platform.retrieval.tools.response_parser import parse_mcp_result
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.citations import (
    ReferenceIndex,
    build_reference_index,
)

logger = logging.getLogger(__name__)


GENERATION_STRATEGY_LABELS = frozenset({"dev_isolation", "lit_and_tools", "lit_only", "no_lit"})
TOOLS_REQUIRING_STRATEGIES = frozenset({"dev_isolation", "lit_and_tools"})


@dataclass
class GenerationCounts:
    tools_count: int
    debate_with_lit_count: int
    debate_only_count: int
    assumptions_count: int = 0
    is_dev_isolation: bool = False
    is_degraded_mode: bool = False

    @property
    def strategy_counts(self) -> dict[str, int]:
        return {
            "tools": self.tools_count,
            "debate_lit": self.debate_with_lit_count,
            "debate_only": self.debate_only_count,
            "assumptions": self.assumptions_count,
        }


def _check_literature_availability(
    articles_with_reasoning: str | None, mcp_available: bool
) -> bool:
    return (
        articles_with_reasoning is not None
        and articles_with_reasoning != LITERATURE_REVIEW_FAILED
        and mcp_available
    )


def _forced_generation_strategy(state: WorkflowState) -> str | None:
    forced = state.get("generation_strategy")
    if isinstance(forced, str) and forced in GENERATION_STRATEGY_LABELS:
        return forced
    return None


def _classify_generation_strategy(
    state: WorkflowState, has_literature: bool, enable_tool_calling: bool
) -> str:
    if state.get("dev_test_lit_tools_isolation", False):
        return "dev_isolation"
    if has_literature:
        return "lit_and_tools" if enable_tool_calling else "lit_only"
    return "no_lit"


def _determine_generation_counts(
    state: WorkflowState,
    total_count: int,
    has_literature: bool,
    enable_tool_calling: bool,
) -> GenerationCounts:
    strategy = _forced_generation_strategy(state) or _classify_generation_strategy(
        state, has_literature, enable_tool_calling
    )
    if strategy == "dev_isolation":
        return GenerationCounts(total_count, 0, 0, is_dev_isolation=True)

    assumptions = total_count // 4 if total_count >= 4 else 0
    remainder = total_count - assumptions
    tools = max(1, remainder // 2) if strategy == "lit_and_tools" else 0
    return GenerationCounts(
        tools_count=tools,
        debate_with_lit_count=(
            remainder - tools if strategy in {"lit_and_tools", "lit_only"} else 0
        ),
        debate_only_count=remainder if strategy == "no_lit" else 0,
        assumptions_count=assumptions,
        is_degraded_mode=strategy == "no_lit",
    )


async def _report_generation_start(
    state: WorkflowState, counts: GenerationCounts, total_count: int
) -> None:
    extra: dict[str, Any] = {}
    if counts.is_dev_isolation:
        logger.info(
            "Dev isolation mode: allocating all hypotheses to lit tools generation (no debate)"
        )
        message = f"Generating {total_count} hypotheses with lit tools only (dev isolation mode)..."
        extra = {"dev_isolation_mode": True}
    elif counts.debate_with_lit_count > 0:
        message = _report_literature_start(counts, total_count)
    elif counts.is_degraded_mode:
        logger.warning(
            "No literature review tools available - generating"
            " hypotheses from model latent knowledge only"
        )
        message = f"Generating {counts.debate_only_count} hypotheses without literature review..."
        extra = {"literature_review_available": False, "degraded_mode": True}
    else:
        # A one-hypothesis literature/tools batch historically emits no start
        # event; preserve that lifecycle.

        return
    await emit_progress(state, "generation_start", message, PROGRESS_GENERATE_START, **extra)


def _report_literature_start(counts: GenerationCounts, total_count: int) -> str:
    if counts.tools_count > 0:
        logger.info(
            "Condition (a): Generating %s hypotheses with literature review "
            "(%s tool-based + %s debate-with-literature)",
            total_count,
            counts.tools_count,
            counts.debate_with_lit_count,
        )
        return (
            f"Generating {total_count} hypotheses"
            f" ({counts.tools_count} tool-based"
            f" + {counts.debate_with_lit_count} debate-with-literature)..."
        )
    logger.info(
        "Condition (c): Generating %s hypotheses with debate-with-literature",
        total_count,
    )
    return f"Generating {total_count} hypotheses with debate-with-literature..."


def _extract_enrichment_payload(parsed: Any, enrichment: EnrichmentConfig) -> Any:
    if enrichment.results_path and isinstance(parsed, dict):
        return parsed.get(enrichment.results_path, parsed)
    return parsed


async def _call_enrichment_tool(
    hyp: Hypothesis,
    enrichment: EnrichmentConfig,
    tool_config: ToolConfig,
    mcp_client: Any,
    semaphore: asyncio.Semaphore,
) -> Any:
    input_value = getattr(hyp, enrichment.input_field, hyp.text)
    async with semaphore:
        result = await mcp_client.call_tool(
            tool_config.mcp_tool_name,
            topic=input_value,
            max_results=enrichment.max_results,
        )
    parsed = parse_mcp_result(result)
    return _extract_enrichment_payload(parsed, enrichment)


async def _enrich_one_hypothesis(
    hyp: Hypothesis,
    enrichment: EnrichmentConfig,
    tool_config: ToolConfig,
    output_key: str,
    mcp_client: Any,
    semaphore: asyncio.Semaphore,
) -> None:
    """Enrichment is supplementary; its failure must not reject the generated
    hypothesis."""
    try:
        hyp.enrichments[output_key] = await _call_enrichment_tool(
            hyp,
            enrichment,
            tool_config,
            mcp_client,
            semaphore,
        )
    except Exception as e:
        logger.warning("enrichment '%s' failed for hypothesis: %s", output_key, e)
        hyp.enrichments[output_key] = {"error": str(e)}


async def _run_one_enrichment(
    enrichment: EnrichmentConfig,
    tool_registry: Any,
    hypotheses: list[Hypothesis],
    mcp_client: Any,
    semaphore: asyncio.Semaphore,
) -> None:
    tool_config = tool_registry.get_tool(enrichment.tool)
    if not tool_config:
        logger.warning("enrichment tool '%s' not found in registry", enrichment.tool)
        return

    output_key = enrichment.output_key or enrichment.tool
    logger.info(
        "running enrichment '%s' via %s for %s hypotheses",
        output_key,
        tool_config.mcp_tool_name,
        len(hypotheses),
    )

    await asyncio.gather(
        *(
            _enrich_one_hypothesis(hyp, enrichment, tool_config, output_key, mcp_client, semaphore)
            for hyp in hypotheses
        )
    )


async def _enrich_hypotheses(
    hypotheses: list[Hypothesis],
    state: WorkflowState,
) -> None:

    tool_registry = state.get("tool_registry")
    if not tool_registry:
        return

    enrichment_configs = tool_registry.get_enrichment_configs()
    if not enrichment_configs:
        return

    mcp_client = await get_mcp_client(tool_registry=tool_registry)
    # A shared semaphore bounds outstanding MCP work across all enrichment
    # configurations.

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)

    await asyncio.gather(
        *(
            _run_one_enrichment(enrichment, tool_registry, hypotheses, mcp_client, semaphore)
            for enrichment in enrichment_configs
        )
    )


@dataclass
class GenerationResults:
    tools_hypotheses: list[Hypothesis]
    debate_with_lit_hypotheses: list[Hypothesis]
    debate_only_hypotheses: list[Hypothesis]

    debate_transcripts: list[dict[str, Any]]

    assumptions_hypotheses: list[Hypothesis] = field(default_factory=list)

    llm_call_count: int = 0

    @property
    def all_hypotheses(self) -> list[Hypothesis]:
        return (
            self.tools_hypotheses
            + self.debate_with_lit_hypotheses
            + self.debate_only_hypotheses
            + self.assumptions_hypotheses
        )


def _unpack_generation_results(
    tasks: list[tuple[str, Coroutine[Any, Any, Any]]],
    results: list[Any],
) -> GenerationResults:
    buckets: dict[str, list[Hypothesis]] = {
        "tools": [],
        "debate_lit": [],
        "debate_only": [],
        "assumptions": [],
    }
    debate_transcripts: list[dict[str, Any]] = []
    llm_call_count = 0
    for (kind, _), result in zip(tasks, results, strict=True):
        if kind in ("debate_lit", "debate_only"):
            hypotheses, transcripts, llm_calls = result
            debate_transcripts.extend(transcripts)
        else:
            hypotheses, llm_calls = result
        buckets[kind] = hypotheses
        llm_call_count += int(llm_calls)
    return GenerationResults(
        tools_hypotheses=buckets["tools"],
        debate_with_lit_hypotheses=buckets["debate_lit"],
        debate_only_hypotheses=buckets["debate_only"],
        debate_transcripts=debate_transcripts,
        assumptions_hypotheses=buckets["assumptions"],
        llm_call_count=llm_call_count,
    )


def _apply_degraded_mode_fallback(hypotheses: list[Hypothesis]) -> None:
    """Overwrite model-written grounding without literature so the displayed
    hypothesis cannot imply nonexistent retrieval."""
    for hyp in hypotheses:
        hyp.literature_grounding = (
            "No literature review available. This hypothesis is based"
            " on the model's latent knowledge and has not been"
            " validated against current research literature."
            " Novelty and scientific validity should be independently"
            " verified."
        )


def _log_bucket_methods(label: str, hypotheses: list[Hypothesis]) -> None:
    if not hypotheses:
        return
    logger.debug(
        "%s generation_methods: %s",
        label,
        [h.generation_method.value if h.generation_method else None for h in hypotheses],
    )


def _log_generation_summary(results: GenerationResults) -> None:
    total = len(results.all_hypotheses)
    logger.info(
        "Generated %s total hypotheses (%s tool-based,"
        " %s debate-with-lit, %s debate-only, %s assumptions)",
        total,
        len(results.tools_hypotheses),
        len(results.debate_with_lit_hypotheses),
        len(results.debate_only_hypotheses),
        len(results.assumptions_hypotheses),
    )

    _log_bucket_methods("tool-based", results.tools_hypotheses)
    _log_bucket_methods("debate-with-Lit", results.debate_with_lit_hypotheses)
    _log_bucket_methods("debate-only", results.debate_only_hypotheses)
    _log_bucket_methods("assumptions", results.assumptions_hypotheses)


async def _emit_complete_progress(
    state: WorkflowState, results: GenerationResults, counts: GenerationCounts
) -> str:
    parts = [
        f"{len(hypotheses)} {label}"
        for count, hypotheses, label in (
            (counts.tools_count, results.tools_hypotheses, "tool-based"),
            (
                counts.debate_with_lit_count,
                results.debate_with_lit_hypotheses,
                "debate-with-literature",
            ),
            (
                counts.debate_only_count,
                results.debate_only_hypotheses,
                "debate-only",
            ),
            (
                counts.assumptions_count,
                results.assumptions_hypotheses,
                "assumptions",
            ),
        )
        if count > 0
    ]
    all_hypotheses = results.all_hypotheses

    message = f"Generated {len(all_hypotheses)} hypotheses ({', '.join(parts)})"

    await emit_progress(
        state,
        "generation_complete",
        message,
        PROGRESS_GENERATE_COMPLETE,
        hypotheses_count=len(all_hypotheses),
    )
    return message


@dataclass(frozen=True)
class GenerationPlan:
    counts: GenerationCounts
    reference_index: ReferenceIndex
    literature: str | None


def _log_reference_index_summary(reference_index: ReferenceIndex) -> None:
    if reference_index.is_empty():
        return

    counts = Counter(s.get("type") for s in reference_index.sources.values())
    logger.info(
        "Built reference index: %s paper(s), %s KG source(s)",
        counts["paper"],
        counts["knowledge_graph"],
    )


async def prepare_generation(
    state: WorkflowState,
) -> GenerationPlan:
    if not state.get("supervisor_guidance"):
        raise GenerationError("No supervisor_guidance in state for node=generation")
    articles_with_reasoning = state.get("articles_with_reasoning")
    total_count = state["initial_hypotheses_count"]
    maximum = (state.get("budget") or {}).get("max_ideas", MAX_PLANNING_IDEAS)
    if type(total_count) is not int or not 0 <= total_count <= min(maximum, MAX_PLANNING_IDEAS):
        raise GenerationError("generation count exceeds the admitted idea budget")
    has_literature = _check_literature_availability(
        articles_with_reasoning, bool(state.get("mcp_available", False))
    )
    counts = _determine_generation_counts(
        state,
        total_count,
        has_literature,
        bool(state.get("enable_tool_calling_generation", False)),
    )

    reference_index = build_reference_index(
        articles=state.get("articles"),
        context_enrichment_sources=state.get("context_enrichment_sources"),
    )
    _log_reference_index_summary(reference_index)

    await _report_generation_start(state, counts, total_count)

    return GenerationPlan(
        counts=counts,
        reference_index=reference_index,
        literature=_with_interim_overview(state, articles_with_reasoning),
    )


def _with_interim_overview(state: WorkflowState, articles_with_reasoning: str | None) -> str | None:
    """The run's own overview is guidance, not retrieved literature;
    debate-only calls must omit this context."""
    block = str(state.get("interim_overview") or "").strip()
    if not block:
        return articles_with_reasoning
    if not articles_with_reasoning:
        return block
    return f"{block}\n\n{articles_with_reasoning}"


def _stamp_generation_lineage(hypotheses: list[Hypothesis], creation_iteration: int) -> None:
    """Expansion cycles preserve the underlying method as provenance;
    generated roots retain parent_id=None."""
    for hyp in hypotheses:
        hyp.creation_iteration = creation_iteration
        if creation_iteration > 0:
            if hyp.generation_method is not None:
                hyp.enrichments["base_generation_method"] = hyp.generation_method.value
            hyp.generation_method = GenerationMethod.RESEARCH_EXPANSION


async def finalize_generation(
    state: WorkflowState,
    counts: GenerationCounts,
    results: GenerationResults,
) -> dict[str, Any]:
    """Only latent debate and assumptions need degraded grounding;
    literature-only strategy buckets cannot occur in that mode."""

    if counts.is_degraded_mode:
        _apply_degraded_mode_fallback(results.debate_only_hypotheses)
        _apply_degraded_mode_fallback(results.assumptions_hypotheses)

    _log_generation_summary(results)
    message_content = await _emit_complete_progress(state, results, counts)

    all_hypotheses = results.all_hypotheses
    _stamp_generation_lineage(all_hypotheses, state.get("current_iteration", 0))

    await _enrich_hypotheses(all_hypotheses, state)

    # Later generation cycles must append to the existing pool.
    return {
        "hypotheses": AppendHypotheses(all_hypotheses),
        "debate_transcripts": results.debate_transcripts,
        "hypothesis_count": len(all_hypotheses),
        "llm_call_count": results.llm_call_count,
        "message": message_content,
    }
