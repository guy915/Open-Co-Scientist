from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from langgraph.graph import add_messages
from typing_extensions import TypedDict

from co_scientist.domains.research_state.models import ExecutionMetrics, Hypothesis, merge_metrics
from co_scientist.platform.retrieval.article import Article

logger = logging.getLogger(__name__)


def accumulate_research_ledgers(
    existing: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep each researcher's ledger separate; replayed tasks must not
    duplicate provenance.
    """
    if not new:
        return existing
    combined = list(existing)
    for ledger in new:
        if ledger not in combined:
            combined.append(ledger)
    return combined


def accumulate_matchups(
    existing: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Deduplicate by pair and pre-match ratings: replay is idempotent,
    genuine later rematches survive.
    """
    if not new:
        return existing
    combined = list(existing)
    seen = {_matchup_identity(item) for item in existing}
    for item in new:
        identity = _matchup_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        combined.append(item)
    return combined


def _matchup_identity(matchup: dict[str, Any]) -> tuple[Any, ...]:
    return (
        matchup.get("hypothesis_a_id"),
        matchup.get("hypothesis_b_id"),
        matchup.get("winner_elo_before"),
        matchup.get("loser_elo_before"),
    )


@dataclasses.dataclass(frozen=True)
class AppendHypotheses:
    """Append evolved children without replacing parents; reject only exact
    identity/text collisions.
    """

    items: list[Hypothesis]


@dataclasses.dataclass(frozen=True)
class ReplaceHypotheses:
    """An explicit empty replacement clears blocked pools; an empty bare list
    means no update.
    """

    items: list[Hypothesis]


HypothesisUpdate = list[Hypothesis] | AppendHypotheses | ReplaceHypotheses


def _dedup_by_id(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Curating nodes own their full pool; repeated IDs keep the first entry
    without fuzzy collapse.
    """
    seen: set[str] = set()
    result: list[Hypothesis] = []
    for hyp in hypotheses:
        if hyp.id not in seen:
            seen.add(hyp.id)
            result.append(hyp)
    return result


def _append_hypotheses(existing: list[Hypothesis], incoming: list[Hypothesis]) -> list[Hypothesis]:
    """Exact collisions are rejected at content admission; near-duplicate
    judgment belongs to proximity.
    """
    seen_ids = {hyp.id for hyp in existing}
    seen_texts = {hyp.text.strip().lower() for hyp in existing}
    result = list(existing)
    for hyp in incoming:
        text_key = hyp.text.strip().lower()
        if hyp.id in seen_ids or text_key in seen_texts:
            logger.debug(
                "append: skipped duplicate hypothesis (id/text): %s...",
                hyp.text[:80],
            )
            continue
        seen_ids.add(hyp.id)
        seen_texts.add(text_key)
        result.append(hyp)
    return result


def deduplicate_hypotheses(existing: list[Hypothesis], new: HypothesisUpdate) -> list[Hypothesis]:
    """Bare empty lists preserve the pool; ReplaceHypotheses can deliberately
    clear it.
    """
    if isinstance(new, AppendHypotheses):
        return _append_hypotheses(existing, new.items)
    if isinstance(new, ReplaceHypotheses):
        return _dedup_by_id(new.items)
    # Bare empty lists mean no update; explicit replacement is required to clear
    # the pool.
    if not new:
        return existing
    return _dedup_by_id(new)


# Annotated channels use their reducers; unannotated channels deliberately use
# last-write-wins.
class WorkflowState(TypedDict):
    research_goal: str

    model_name: str

    supervisor_model_name: str

    max_iterations: int

    initial_hypotheses_count: int

    evolution_max_count: int

    tournament_pairs: int

    elo_k_factor: int

    literature_review_papers_count: int

    hypotheses: Annotated[list[Hypothesis], deduplicate_hypotheses]

    current_iteration: int

    resume: bool | None
    # Restored state marks continuation; fresh setup must not replay committed
    # science.

    task_history: list[dict[str, Any]]

    next_task: str | None

    next_task_priority: int

    durable_task_queue: list[dict[str, Any]]

    supervisor_queue_actions: list[dict[str, Any]]

    termination_reason: str | None

    budget: dict[str, Any] | None

    orchestrator_state: dict[str, Any]

    supervisor_decision_provenance: str | None

    pending_steering: bool | None
    # Consume durable steering at the next safe scheduling boundary.

    durable_retries_remain: bool | None
    # Retry rights belong to this attempt; absent rights mean degrade safely.

    supervisor_guidance: dict[str, Any]

    meta_review: dict[str, Any]

    research_overview: dict[str, Any] | None

    removed_duplicates: list[dict[str, Any]]

    proximity_graph: dict[str, Any]

    tournament_matchups: Annotated[list[dict[str, Any]], accumulate_matchups]

    pending_ranking_matchups: list[dict[str, Any]]

    evolution_details: list[dict[str, Any]]

    safety_decisions: list[dict[str, Any]]

    safety_blocked: bool | None
    # A mid-flight hard stop forbids further science before any later scheduling
    # boundary.

    held_for_review: list[dict[str, Any]]
    # Held hypotheses leave the active pool but retain full records for
    # contextual review.

    degraded_nodes: list[str]
    # Distinguish generated placeholders from deliberately unattempted retrieval
    # capabilities.

    retrieval_degradation: dict[str, Any] | None
    # Missing retrieval is otherwise invisible even when the workflow completes
    # normally.

    metrics: Annotated[ExecutionMetrics, merge_metrics]

    start_time: float

    run_id: str

    progress_callback: None | (Callable[[str, dict[str, Any]], Awaitable[None]])

    # LangGraph add_messages performs ID-based append/merge, unlike custom state
    # reducers.
    messages: Annotated[list[dict[str, Any]], add_messages]

    preferences: str | None

    attributes: list[str] | None

    constraints: list[str] | None

    lab_constraints: list[str] | None
    # Empty lab constraints leave feasibility prompts unchanged.

    criteria: list[str] | None

    run_focus_guidance: str | None

    run_setup_guidance: str | None

    starting_hypotheses: list[str] | None

    literature: list[str] | None

    articles_with_reasoning: str | None

    literature_review_queries: list[str] | None

    articles: list[Article] | None

    debate_transcripts: list[dict[str, Any]] | None

    mcp_available: bool | None

    pubmed_available: bool | None

    enable_tool_calling_generation: bool | None

    enable_simulation_execution: bool | None
    # A request cannot bypass missing sandbox confinement.

    enable_overview_review: bool | None
    # Offline models cannot perform the requested scientific validation.

    enable_meta_review: bool | None
    # Disables periodic cadence only; EVOLVE still needs meta-review critique.

    generation_strategy: str | None
    # Ablations may fix the strategy; upstream validation rejects unavailable
    # tool requirements.

    research_tier: str | None

    research_expansion_findings: str | None
    # Prompt-only cycle material; the research ledger carries its durable
    # provenance.

    research_ledgers: Annotated[list[dict[str, Any]], accumulate_research_ledgers]
    # Cross-checkpoint provenance must remain plain JSON.

    interim_overview: str | None
    # Each firing replaces the prior guidance, so last-write-wins is
    # intentional.

    dev_test_lit_tools_isolation: bool | None

    dev_mode: bool | None

    tool_registry: Any | None

    context_enrichment_sources: list[dict[str, Any]] | None
