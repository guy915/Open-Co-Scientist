import logging
import time
from dataclasses import dataclass
from typing import Any

from co_scientist.core.constants import (
    LONG_MAX_TOKENS,
    LOW_TEMPERATURE,
    PROGRESS_PROXIMITY_COMPLETE,
    PROGRESS_PROXIMITY_START,
)
from co_scientist.core.prompt_layout import append_item_context
from co_scientist.domains.research_state.models import (
    Hypothesis,
    MetricDeltas,
    create_metrics_update,
    phase_message,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
)
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.node_degradation import run_or_degrade
from co_scientist.science.prompts import get_proximity_prompt
from co_scientist.science.prompts._common import _format_meta_review_context
from co_scientist.science.proximity.proximity_dedup import (
    _assign_cluster_ids,
    _dedupe_by_cluster,
)
from co_scientist.science.proximity.proximity_graph import (
    SurvivorIndex,
    build_proximity_graph,
    member_match_key,
)

logger = logging.getLogger(__name__)


def _prepare_hypotheses_for_analysis(
    hypotheses: list[Hypothesis],
) -> list[dict[str, Any]]:
    """Keep full text: methodology, assumptions and applications may differ
    in tails, and false high similarity permanently removes distinct work."""
    return [
        {
            "text": hyp.text,
            "score": hyp.score,
            "elo_rating": hyp.elo_rating,
            "index": i,
        }
        for i, hyp in enumerate(hypotheses)
    ]


def _meta_review_section(state: WorkflowState) -> str:
    """Shared builder output stays exact; appended critique steers which
    directions remain distinct and is absent before the first synthesis."""
    section = _format_meta_review_context(state.get("meta_review"))
    return f"\n{section}" if section else ""


async def _fetch_similarity_clusters(
    state: WorkflowState, hypotheses: list[Hypothesis]
) -> list[dict[str, Any]]:
    hypotheses_for_analysis = _prepare_hypotheses_for_analysis(hypotheses)
    supervisor_guidance = state.get("supervisor_guidance")

    prompt, schema = get_proximity_prompt(
        hypotheses_for_analysis, supervisor_guidance=supervisor_guidance
    )
    prompt = append_item_context(prompt, _meta_review_section(state))

    response = await call_llm_json(
        prompt=prompt,
        spec=CompletionSpec(
            role="proximity",
            model_name=state["model_name"],
            max_tokens=LONG_MAX_TOKENS,
            temperature=LOW_TEMPERATURE,
            json_schema=schema,
        ),
        options=LLMCallOptions(
            run_id=state.get("run_id"),
            prompt_name="proximity",
        ),
    )

    similarity_clusters: list[dict[str, Any]] = response.get("similarity_clusters", [])
    return similarity_clusters


@dataclass
class _ClusteringOutcome:
    hypotheses_to_keep: list[Hypothesis]
    removed_duplicates: list[dict[str, Any]]
    similarity_clusters: list[dict[str, Any]]


def _finish_clustering(
    hypotheses: list[Hypothesis], similarity_clusters: list[dict[str, Any]]
) -> _ClusteringOutcome:
    _assign_cluster_ids(hypotheses, similarity_clusters)
    hypotheses_to_keep, removed_duplicates = _dedupe_by_cluster(hypotheses)
    # One info summary suffices; individual drops already have debug traces
    # and archive records, avoiding crowding the bounded readable log.
    logger.info(
        "Proximity analysis complete: %s → %s hypotheses (%s duplicates removed)",
        len(hypotheses),
        len(hypotheses_to_keep),
        len(removed_duplicates),
    )
    return _ClusteringOutcome(hypotheses_to_keep, removed_duplicates, similarity_clusters)


async def _run_proximity_clustering(
    state: WorkflowState,
    hypotheses: list[Hypothesis],
) -> "_ClusteringOutcome | None":
    await emit_progress(
        state,
        "proximity_start",
        f"Analyzing similarity of {len(hypotheses)} hypotheses...",
        PROGRESS_PROXIMITY_START,
    )

    similarity_clusters = await _fetch_similarity_clusters(state, hypotheses)

    if not similarity_clusters:
        logger.warning("No similarity clusters returned, skipping deduplication")
        return None

    return _finish_clustering(hypotheses, similarity_clusters)


def _survivor_index(hypotheses: list[Hypothesis], outcome: _ClusteringOutcome) -> SurvivorIndex:
    """Do not renumber survivors: response indices refer to the original
    prompt and dropped members must resolve to nothing."""
    kept_ids = {h.id for h in outcome.hypotheses_to_keep}
    return SurvivorIndex(
        by_index={index: hyp.id for index, hyp in enumerate(hypotheses) if hyp.id in kept_ids},
        by_text={member_match_key(h.text): h.id for h in outcome.hypotheses_to_keep},
        texts={h.id: h.text for h in outcome.hypotheses_to_keep},
    )


def _build_proximity_update(
    state: WorkflowState,
    hypotheses: list[Hypothesis],
    outcome: _ClusteringOutcome,
) -> dict[str, Any]:
    """Bare subset lists replace the pool through its reducer; accumulate
    duplicate history and leave iteration ownership to the orchestrator."""
    metrics = create_metrics_update(deltas=MetricDeltas(llm_calls=1))
    all_removed_duplicates = state.get("removed_duplicates", []) + outcome.removed_duplicates
    proximity_graph = build_proximity_graph(
        outcome.similarity_clusters,
        _survivor_index(hypotheses, outcome),
        research_goal=state["research_goal"],
        model=state["model_name"],
        updated_at=time.time(),
    )

    return {
        "hypotheses": outcome.hypotheses_to_keep,
        "removed_duplicates": all_removed_duplicates,
        "proximity_graph": proximity_graph,
        "metrics": metrics,
        "messages": phase_message(
            "proximity",
            f"Deduplication: {len(hypotheses)}"
            f" → {len(outcome.hypotheses_to_keep)}"
            f" ({len(outcome.removed_duplicates)} removed)",
            duplicates_removed=len(outcome.removed_duplicates),
            clusters=len(outcome.similarity_clusters),
        ),
    }


async def proximity_node(state: WorkflowState) -> dict[str, Any]:
    hypotheses = state["hypotheses"]
    logger.info("Analyzing proximity of %s hypotheses", len(hypotheses))

    if len(hypotheses) <= 1:
        logger.info("Not enough hypotheses for proximity analysis")
        return {"hypotheses": hypotheses}

    # Bad clustering or exhausted retrieval must degrade deduplication rather
    # than lose the report by failing the durable enhancement task.
    return await run_or_degrade(
        state,
        lambda: _cluster_and_dedup(state, hypotheses),
        schema_name="proximity_analysis",
        fallback=lambda: {"hypotheses": hypotheses},
        lost="this cycle keeps its near-duplicate hypotheses",
    )


async def _cluster_and_dedup(state: WorkflowState, hypotheses: list[Hypothesis]) -> dict[str, Any]:
    outcome = await _run_proximity_clustering(state, hypotheses)
    if outcome is None:
        return {"hypotheses": hypotheses}

    await emit_progress(
        state,
        "proximity_complete",
        f"Removed {len(outcome.removed_duplicates)} duplicates",
        PROGRESS_PROXIMITY_COMPLETE,
        duplicates_removed=len(outcome.removed_duplicates),
        remaining=len(outcome.hypotheses_to_keep),
    )

    return _build_proximity_update(state, hypotheses, outcome)
