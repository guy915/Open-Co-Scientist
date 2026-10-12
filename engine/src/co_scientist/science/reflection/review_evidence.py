from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import weakref
from collections.abc import Callable, Coroutine
from typing import Any, NamedTuple

from co_scientist.core.constants import (
    DEFAULT_MAX_TOKENS,
    LITERATURE_REVIEW_MAX_QUERIES,
    LOW_TEMPERATURE,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.models import Hypothesis, rank_by_elo
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
)
from co_scientist.platform.retrieval.article import Article
from co_scientist.platform.retrieval.evidence.article_support import (
    build_articles_from_metadata,
    records_from_findings,
)
from co_scientist.platform.retrieval.research import (
    ResearchBudget,
    conduct_research,
    result_to_dict,
)
from co_scientist.platform.retrieval.research_adapter import (
    McpRetrieval,
    review_budget_for_tier,
    reviewed_hypothesis_limit,
)
from co_scientist.science.evidence_context import (
    _retrieve_probe_evidence,
)
from co_scientist.science.prompts import (
    get_hypothesis_query_generation_prompt,
)
from co_scientist.science.reflection.review_gate import ReviewType
from co_scientist.science.research_model import LlmResearchModel
from co_scientist.science.schemas import LITERATURE_QUERY_SCHEMA

logger = logging.getLogger(__name__)


# Supported assumptions are settled; researching them only buys confirmation.
_UNSETTLED_SUPPORT = ("uncertain", "likely_false")

# The claim rides into every planned question; bound repeated prompt cost.
_MAX_GOAL_CHARS = 1200


class ReviewResearch(NamedTuple):
    articles: list[Article]
    ledger: dict[str, Any]


async def research_for_review(
    state: WorkflowState, hypothesis: Hypothesis
) -> ReviewResearch | None:
    prepared = await _prepare(state, hypothesis)
    if prepared is None:
        return None
    retrieval, budget = prepared

    result = await conduct_research(
        goal=_research_goal(state, hypothesis),
        model=LlmResearchModel(
            str(state.get("model_name") or ""),
            run_id=str(state.get("run_id") or ""),
        ),
        retrieval=retrieval,
        budget=budget,
        seed_questions=_seed_questions(hypothesis, budget.breadth),
    )
    logger.info(
        "Review research for %s: %s threads, %s calls, %s findings (%s)",
        hypothesis.id,
        len(result.threads),
        len(result.calls),
        len(result.findings),
        result.stop_reason.value,
    )
    records = records_from_findings(result, retrieval)
    return ReviewResearch(
        articles=build_articles_from_metadata(records, ""),
        ledger=result_to_dict(result),
    )


async def _prepare(
    state: WorkflowState, hypothesis: Hypothesis
) -> tuple[McpRetrieval, ResearchBudget] | None:
    from co_scientist.science.evidence_context import (
        _probe_search_config,
    )

    tier = str(state.get("research_tier") or "")
    if not tier or not state.get("mcp_available"):
        return None
    if hypothesis.id not in _researched_hypothesis_ids(state, tier):
        return None
    config = _probe_search_config(state)
    if config.workflow is None or config.tool_registry is None:
        return None
    retrieval = await McpRetrieval.open_for(config, str(state.get("run_id") or ""))
    budget = review_budget_for_tier(tier, retrieval.sources)
    return None if budget is None else (retrieval, budget)


def _researched_hypothesis_ids(state: WorkflowState, tier: str) -> set[str]:
    """Whole-pool selection aligns durable and node callers; canonical
    ranking breaks ties with initial reviews before tournament Elo exists."""
    limit = reviewed_hypothesis_limit(tier)
    if limit < 1:
        return set()
    viable = [
        hypothesis
        for hypothesis in state.get("hypotheses") or []
        if hypothesis.review_disposition == "viable"
    ]
    return {hypothesis.id for hypothesis in rank_by_elo(viable)[:limit]}


def _research_goal(state: WorkflowState, hypothesis: Hypothesis) -> str:
    """Each question needs both the claim and run domain; either alone loses
    its target."""
    claim = " ".join((hypothesis.text or "").split())[:_MAX_GOAL_CHARS]
    return (
        f"{state.get('research_goal') or ''}\n\nSpecifically, the claim under review: {claim}"
    ).strip()


def _seed_questions(hypothesis: Hypothesis, limit: int) -> list[str]:
    seeds: list[str] = []
    seen: set[str] = set()
    for doubt in _unsettled(hypothesis):
        if doubt.lower() in seen:
            continue
        seen.add(doubt.lower())
        seeds.append(doubt)
        if len(seeds) >= limit:
            break
    return seeds


def _unsettled(hypothesis: Hypothesis) -> list[str]:
    full = hypothesis.enrichments.get("full")
    simulation = hypothesis.enrichments.get("simulation")
    doubts = _unsettled_assumptions(full)
    if isinstance(simulation, dict):
        doubts.extend(
            " ".join(str(point).split())
            for point in simulation.get("failure_points") or []
            if str(point).strip()
        )
    return doubts


def _unsettled_assumptions(review: object) -> list[str]:
    if not isinstance(review, dict):
        return []
    return [
        " ".join(str(item.get("assumption")).split())
        for item in review.get("assumptions") or []
        if isinstance(item, dict)
        and item.get("support") in _UNSETTLED_SUPPORT
        and str(item.get("assumption") or "").strip()
    ]


_MAX_HYPOTHESIS_QUERIES = LITERATURE_REVIEW_MAX_QUERIES


async def _hypothesis_search_queries(state: WorkflowState, hypothesis: Hypothesis) -> list[str]:
    """Keyword backends AND terms, so whole-goal/claim prose would match
    nothing."""
    # Without MCP, keyword queries still ground against the existing corpus.
    if not state.get("mcp_available") and not state.get("articles"):
        return []
    result = await _call_hypothesis_query_llm(state, hypothesis)
    if result is None:
        return []
    queries = [
        " ".join(str(query).split()) for query in result.get("queries") or [] if str(query).strip()
    ]
    return queries[:_MAX_HYPOTHESIS_QUERIES]


async def _call_hypothesis_query_llm(
    state: WorkflowState, hypothesis: Hypothesis
) -> dict[str, Any] | None:
    try:
        return await call_llm_json(
            prompt=get_hypothesis_query_generation_prompt(
                research_goal=state["research_goal"],
                hypothesis=hypothesis.text,
            ),
            spec=CompletionSpec(
                role="evidence_queries",
                model_name=state["model_name"],
                max_tokens=DEFAULT_MAX_TOKENS,
                temperature=LOW_TEMPERATURE,
                json_schema=LITERATURE_QUERY_SCHEMA,
            ),
            options=LLMCallOptions(
                run_id=state.get("run_id"),
                prompt_name=f"hypothesis_queries_{hypothesis.id}",
            ),
        )
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        logger.warning("Query generation failed for %s: %s", hypothesis.id, exc)
        return None


# Tasks belong to their event loop; weak keys prevent finished cohorts retaining
# retrievals or sharing asyncio objects across threads.
_review_evidence_flights: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Task[_ReviewEvidence]]
] = weakref.WeakKeyDictionary()


@dataclasses.dataclass(frozen=True)
class _ReviewEvidence:
    """The ledger has a run-level writer; embedding it in each review would
    duplicate provenance across checkpoint envelopes."""

    queries: list[str]
    articles: list[Article]
    errors: list[str]
    ledger: dict[str, Any] | None = None


def _evidence_key(state: WorkflowState, hypothesis: Hypothesis) -> str:
    """Include claim text: evidence for an earlier wording may not support
    its replacement."""
    text = " ".join((hypothesis.text or "").split())
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{state.get('run_id')}:{hypothesis.id}:{digest}"


async def _shared_review_evidence(state: WorkflowState, hypothesis: Hypothesis) -> _ReviewEvidence:
    """Concurrent full/simulation reviews share one flight to avoid duplicate
    spend; evict failed flights so retries get a fresh attempt."""
    return await _shared_flight(state, hypothesis, _retrieve_review_evidence)


async def finalist_review_evidence(state: WorkflowState, hypothesis: Hypothesis) -> _ReviewEvidence:
    """Targeted retrieval belongs to verification, which searches with the
    review's own queries; deep tiers still fund their research loop."""
    return await _shared_flight(state, hypothesis, _retrieve_finalist_evidence)


async def _shared_flight(
    state: WorkflowState,
    hypothesis: Hypothesis,
    retrieve: Callable[[WorkflowState, Hypothesis], Coroutine[Any, Any, _ReviewEvidence]],
) -> _ReviewEvidence:
    loop = asyncio.get_running_loop()
    flights = _review_evidence_flights.setdefault(loop, {})
    key = _evidence_key(state, hypothesis)
    flight = flights.get(key)
    if flight is None:
        flight = loop.create_task(retrieve(state, hypothesis))
        flights[key] = flight
    try:
        return await flight
    except Exception:
        flights.pop(key, None)
        raise


async def _retrieve_finalist_evidence(
    state: WorkflowState, hypothesis: Hypothesis
) -> _ReviewEvidence:
    research = await _research_for_review(state, hypothesis)
    if research is None:
        return _ReviewEvidence([], [], [])
    return _ReviewEvidence([], research.articles, [], research.ledger)


async def _retrieve_review_evidence(
    state: WorkflowState, hypothesis: Hypothesis
) -> _ReviewEvidence:
    """Research adds depth to probes rather than replacing the evidence
    already funded."""
    queries = await _hypothesis_search_queries(state, hypothesis)
    articles, errors = await _retrieve_probe_evidence(state, queries)
    research = await _research_for_review(state, hypothesis)
    if research is None:
        return _ReviewEvidence(queries, articles, errors)
    known = {article.source_id for article in articles}
    found = [article for article in research.articles if article.source_id not in known]
    return _ReviewEvidence(queries, articles + found, errors, research.ledger)


async def _research_for_review(
    state: WorkflowState, hypothesis: Hypothesis
) -> ReviewResearch | None:
    """Broken optional research must not discard the probe evidence or fail
    the review."""
    try:
        return await research_for_review(state, hypothesis)
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        logger.warning("Review research failed for %s: %s", hypothesis.id, exc)
        return None


async def _review_evidence_for(
    state: WorkflowState, hypothesis: Hypothesis, review_type: ReviewType
) -> _ReviewEvidence:
    """Recurrent reviews reuse tournament context rather than buying another
    search."""
    if review_type not in {ReviewType.FULL, ReviewType.SIMULATION}:
        return _ReviewEvidence([], [], [])
    return await _shared_review_evidence(state, hypothesis)


def researched_articles_for(state: WorkflowState, hypothesis: Hypothesis) -> list[Article]:
    """Verification reuses funded research; new retrieval here would multiply
    spend by pool size and iteration."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return []
    flight = _review_evidence_flights.get(loop, {}).get(_evidence_key(state, hypothesis))
    if flight is None or not flight.done() or flight.cancelled():
        return []
    if flight.exception() is not None:
        return []
    evidence = flight.result()
    return list(evidence.articles) if evidence.ledger is not None else []


def with_researched(
    state: WorkflowState,
    hypothesis: Hypothesis,
    probed: list[Article],
) -> list[Article]:
    """Reuse funded research without new retrieval; duplicate sources waste
    budget and imply independent agreement."""
    known = {article.source_id for article in probed}
    return probed + [
        article
        for article in researched_articles_for(state, hypothesis)
        if article.source_id not in known
    ]
