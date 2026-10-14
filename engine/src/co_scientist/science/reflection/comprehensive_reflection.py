from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any, NamedTuple

from co_scientist.core.constants import (
    EXTENDED_MAX_TOKENS,
    LOW_TEMPERATURE,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.core.prompt_layout import prepend_instructions
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
from co_scientist.platform.retrieval.article import Article
from co_scientist.science.evidence_context import (
    EvidenceCaps,
    build_evidence_context,
    showable_articles,
)
from co_scientist.science.prompts import build_tool_instructions
from co_scientist.science.prompts._common import _format_meta_review_context
from co_scientist.science.prompts.context_budget import select_evidence_excerpt
from co_scientist.science.prompts.loading import load_prompt_with_schema
from co_scientist.science.reflection.deep_verification import (
    merge_retrieved_articles,
)
from co_scientist.science.reflection.reflection import apply_observation_result
from co_scientist.science.reflection.review_evidence import (
    _review_evidence_for,
    _ReviewEvidence,
    finalist_review_evidence,
)
from co_scientist.science.reflection.review_gate import (
    RECHECK_REVIEW_TYPE,
    VERIFICATION_QUERIES_KEY,
    ReviewType,
    finalist_review_needed,
    mark_recheck_issued,
    prompt_name_for,
    recheck_targets,
    store_mature_review_result,
)
from co_scientist.science.reflection.simulation_execution import (
    simulation_observations,
)
from co_scientist.science.scheduling.funnel import finalists, has_depth, is_terminal_depth_pass
from co_scientist.science.schemas.finalist_review import FINALIST_REVIEW_MAX_QUERIES

logger = logging.getLogger(__name__)


# Public and private caps are separate so a full corpus cannot crowd out
# scientist context.
_MAX_REVIEW_ARTICLES = 12

_MAX_REVIEW_PRIVATE_SOURCES = 4


# An empty section implies execution observed nothing; explicitly distinguish
# never running.
_NO_EXECUTION_NOTE = (
    "No simulation was executed for this review. Step through the mechanism yourself."
)

_NO_OBSERVATIONS_NOTE = (
    "No literature analyses are available for this run. Omit `observation` entirely."
)

# Three reviews' answers share one completion.
_FINALIST_REVIEW_MAX_TOKENS = 2 * EXTENDED_MAX_TOKENS


def _prompt_variables(
    state: WorkflowState,
    hypothesis: Hypothesis,
    review_type: ReviewType,
    targeted_articles: list[Article] | None = None,
    observations: str | None = None,
) -> dict[str, str]:
    registry = state.get("tool_registry")
    tool_ids = registry.get_tools_for_workflow("reflection") if registry else []
    tool_instructions = build_tool_instructions(tool_ids, registry)
    hypothesis_text = hypothesis.text
    if review_type is ReviewType.RECURRENT:
        hypothesis_text += _recurrent_review_suffix(state, hypothesis)
    domain_context = _build_domain_context(state, targeted_articles)
    return {
        "research_goal": state["research_goal"],
        "hypothesis_text": hypothesis_text,
        "domain_context": (
            "Evidence available to this review:\n" + domain_context
            if domain_context
            else "No retrieved evidence is available to this review."
        ),
        "tool_instructions": tool_instructions,
        "execution_observations": (
            "A simulation of this mechanism was built and run. What it reported:\n\n" + observations
            if observations
            else _NO_EXECUTION_NOTE
        ),
    }


def _recurrent_review_suffix(state: WorkflowState, hypothesis: Hypothesis) -> str:
    tournament = {
        "elo_rating": hypothesis.elo_rating,
        "match_count": hypothesis.total_matches,
        "reviews": [asdict(review) for review in hypothesis.reviews],
    }
    return (
        "\n\nRecurrent-review context from the growing tournament:\n"
        + json.dumps(tournament, indent=2)
        + "\n\nCross-agent feedback:\n"
        + _format_meta_review_context(state.get("meta_review"))
    )


def _build_domain_context(state: WorkflowState, targeted_articles: list[Article] | None) -> str:
    """Source counts bound distinct voices; reviews weigh a few papers in
    depth."""
    evidence = [
        # Only corpus sources are marked analyzed; filter targeted sources
        # separately. The shared cap preserves corpus-first selection.
        *showable_articles(state.get("articles")),
        *showable_articles(targeted_articles, require_analyzed=False),
    ]
    return build_evidence_context(
        evidence,
        private_sources=state.get("context_enrichment_sources"),
        caps=EvidenceCaps(
            articles=_MAX_REVIEW_ARTICLES,
            private_sources=_MAX_REVIEW_PRIVATE_SOURCES,
        ),
        require_analyzed=False,
    )


class ReviewRun(NamedTuple):
    """The hypothesis writer persists the review; the run writer persists its
    ledger."""

    review_type: ReviewType
    result: dict[str, Any] | None
    ledger: dict[str, Any] | None


async def review_hypothesis(
    state: WorkflowState,
    hypothesis: Hypothesis,
    review_type: ReviewType,
) -> ReviewRun:
    """Bad answers fail this review; worker-owned parking and spend
    exhaustion propagate instead of spending item retries."""
    evidence = await _review_evidence_for(state, hypothesis, review_type)
    targeted_articles = evidence.articles
    observations = await _observations_for(state, hypothesis, review_type)
    prompt, schema = _build_review_prompt(
        state, hypothesis, review_type, targeted_articles, observations
    )
    try:
        result = await call_llm_json(
            prompt=prompt,
            spec=CompletionSpec(
                role="review",
                model_name=state["model_name"],
                max_tokens=EXTENDED_MAX_TOKENS,
                temperature=LOW_TEMPERATURE,
                json_schema=schema,
            ),
            options=LLMCallOptions(
                run_id=state.get("run_id"),
                prompt_name=f"reflection_{review_type.value}_{hypothesis.id}",
            ),
        )
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        logger.error("%s review failed for %s: %s", review_type.value, hypothesis.id, exc)
        return ReviewRun(review_type, None, evidence.ledger)
    _record_review_provenance(result, evidence, targeted_articles, review_type, observations)
    return ReviewRun(review_type, result, evidence.ledger)


def _record_review_provenance(
    result: dict[str, Any],
    evidence: _ReviewEvidence,
    targeted_articles: list[Article],
    review_type: ReviewType,
    observations: str | None,
) -> None:
    """Checkpoint retrieval provenance; the caller attests execution because
    model self-report is unreliable."""
    result["retrieval_queries"] = evidence.queries
    result["retrieval_errors"] = evidence.errors
    result["retrieved_articles"] = [article.to_dict() for article in targeted_articles]
    if review_type is not ReviewType.SIMULATION:
        return
    result["executed"] = observations is not None
    if observations is not None:
        result["execution_observations"] = observations


async def _observations_for(
    state: WorkflowState, hypothesis: Hypothesis, review_type: ReviewType
) -> str | None:
    """A tool loop per hypothesis multiplies cost; require tier opt-in and
    confinement."""
    if review_type is not ReviewType.SIMULATION:
        return None
    if not state.get("enable_simulation_execution"):
        return None
    return await simulation_observations(state, hypothesis)


def _build_review_prompt(
    state: WorkflowState,
    hypothesis: Hypothesis,
    review_type: ReviewType,
    targeted_articles: list[Article],
    observations: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    template_type = ReviewType.FULL if review_type is ReviewType.RECURRENT else review_type
    prompt, schema = load_prompt_with_schema(
        prompt_name_for(template_type),
        _prompt_variables(state, hypothesis, review_type, targeted_articles, observations),
    )
    if review_type is ReviewType.RECURRENT:
        prompt = prepend_instructions(
            prompt,
            "Perform a recurrent/tournament review. Adapt the full review to "
            "the accumulated reviews, Elo outcomes, recurring issues, and "
            "meta-review feedback below. Identify what changed since the "
            "earlier review.\n\n",
        )
    return prompt, schema


async def review_finalist(state: WorkflowState, hypothesis: Hypothesis) -> ReviewRun:
    """Observation, full and simulation reviews in one call; a bad answer
    fails all three together, as one recorded unreviewed finding."""
    evidence = await finalist_review_evidence(state, hypothesis)
    observations = await _observations_for(state, hypothesis, ReviewType.SIMULATION)
    literature = state.get("articles_with_reasoning")
    variables = _prompt_variables(
        state, hypothesis, ReviewType.FULL, evidence.articles, observations
    )
    variables["articles_with_reasoning"] = (
        "Literature analyses, one per article, each with the literature review's own"
        " reasoning:\n" + select_evidence_excerpt(literature, state["research_goal"], 6_000)
        if literature
        else _NO_OBSERVATIONS_NOTE
    )
    variables["domain_reflection_guidance"] = ""
    prompt, schema = load_prompt_with_schema(prompt_name_for(ReviewType.FINALIST), variables)
    try:
        result = await call_llm_json(
            prompt=prompt,
            spec=CompletionSpec(
                role="review",
                model_name=state["model_name"],
                max_tokens=_FINALIST_REVIEW_MAX_TOKENS,
                temperature=LOW_TEMPERATURE,
                json_schema=schema,
            ),
            options=LLMCallOptions(
                run_id=state.get("run_id"),
                prompt_name=f"reflection_finalist_{hypothesis.id}",
            ),
        )
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        logger.error("Finalist review failed for %s: %s", hypothesis.id, exc)
        return ReviewRun(ReviewType.FINALIST, None, evidence.ledger)
    if not literature:
        # Without analyzed literature an observation verdict has no basis.
        result.pop("observation", None)
    _record_review_provenance(
        result, evidence, evidence.articles, ReviewType.SIMULATION, observations
    )
    return ReviewRun(ReviewType.FINALIST, result, evidence.ledger)


def store_finalist_review(hypothesis: Hypothesis, result: dict[str, Any], iteration: int) -> None:
    """Each part goes through its standalone write path, so gates, ranking
    and the report read the same keys and shapes as separate reviews."""
    observation = result.get("observation")
    if isinstance(observation, dict) and not hypothesis.reflection_notes:
        apply_observation_result(hypothesis, observation)
    provenance = {
        key: result[key]
        for key in ("retrieval_queries", "retrieval_errors", "retrieved_articles")
        if key in result
    }
    full = _part(result, "full_review")
    simulation = _part(result, "simulation")
    for key in ("executed", "execution_observations"):
        if key in result:
            simulation[key] = result[key]
    store_mature_review_result(hypothesis, ReviewType.FULL, {**full, **provenance}, iteration)
    store_mature_review_result(hypothesis, ReviewType.SIMULATION, simulation, iteration)
    queries = [
        " ".join(str(query).split())
        for query in result.get(VERIFICATION_QUERIES_KEY) or []
        if str(query).strip()
    ]
    hypothesis.enrichments[VERIFICATION_QUERIES_KEY] = queries[:FINALIST_REVIEW_MAX_QUERIES]


def _part(result: dict[str, Any], key: str) -> dict[str, Any]:
    """A malformed part is unreviewed, not a pass: the verdict gates read
    must never be invented."""
    part = result.get(key)
    if isinstance(part, dict):
        return dict(part)
    return {"verdict": "unreviewed", "justification": f"The review returned no {key}."}


def store_failed_finalist_review(hypothesis: Hypothesis, error: str | None, iteration: int) -> None:
    for review_type in (ReviewType.FULL, ReviewType.SIMULATION):
        store_mature_review_result(
            hypothesis,
            review_type,
            {"verdict": "unreviewed", "justification": error},
            iteration,
        )


async def _review_finalist(
    state: WorkflowState, hypothesis: Hypothesis
) -> tuple[int, list[dict[str, Any]]]:
    iteration = int(state.get("current_iteration", 0))
    run = await review_finalist(state, hypothesis)
    if run.result is None:
        store_failed_finalist_review(hypothesis, "The finalist review failed.", iteration)
    else:
        store_finalist_review(hypothesis, run.result, iteration)
        state["articles"] = merge_retrieved_articles(state.get("articles"), [run.result])
    return 1, [run.ledger] if run.ledger is not None else []


_ReviewRun = ReviewRun


_run_review = review_hypothesis


async def _recheck_hypothesis(state: WorkflowState, hypothesis: Hypothesis) -> int:
    """Issuing spends the one recheck even when the call fails."""
    mark_recheck_issued(hypothesis)
    run = await review_hypothesis(state, hypothesis, RECHECK_REVIEW_TYPE)
    if run.result is None:
        return 0
    store_mature_review_result(
        hypothesis,
        run.review_type,
        run.result,
        int(state.get("current_iteration", 0)),
    )
    return 1


async def _run_blocked_rechecks(state: WorkflowState, hypotheses: list[Hypothesis]) -> int:
    targets = recheck_targets(hypotheses)
    if not targets:
        return 0
    results = await asyncio.gather(
        *[_recheck_hypothesis(state, hypothesis) for hypothesis in targets]
    )
    return sum(results)


async def comprehensive_reflection_node(state: WorkflowState) -> dict[str, Any]:
    """Blocked ideas need their own recheck arm: the viable-only cascade
    cannot reach them to produce a deeper verdict. Each viable finalist gets
    one in-depth review; the terminal pass reviews no blocked idea."""
    hypotheses = state["hypotheses"]
    terminal = is_terminal_depth_pass(state)
    pending = [
        hypothesis
        for hypothesis in finalists(state)
        if hypothesis.review_disposition == "viable"
        and finalist_review_needed(hypothesis)
        and not (terminal and has_depth(hypothesis))
    ]
    # These review arms have no data dependency; overlap their model latency.
    reviewed, recheck_calls = await asyncio.gather(
        asyncio.gather(*[_review_finalist(state, h) for h in pending]),
        _run_blocked_rechecks(state, [] if terminal else hypotheses),
    )
    calls = sum(count for count, _ in reviewed) + recheck_calls
    return {
        "hypotheses": hypotheses,
        "articles": state.get("articles") or [],
        "research_ledgers": [ledger for _, found in reviewed for ledger in found],
        "metrics": create_metrics_update(deltas=MetricDeltas(llm_calls=calls)),
        "messages": phase_message(
            "reflection",
            f"Completed {calls} finalist or recheck reviews",
        ),
    }
