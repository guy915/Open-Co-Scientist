import asyncio
import dataclasses
import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

from co_scientist.core.constants import (
    EXTENDED_MAX_TOKENS,
    LOW_TEMPERATURE,
    MAX_CONCURRENT_LLM_CALLS,
    PROGRESS_DEEP_VERIFICATION_COMPLETE,
    PROGRESS_DEEP_VERIFICATION_START,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.models import (
    Hypothesis,
    MetricDeltas,
    create_metrics_update,
    phase_message,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import CompletionSpec, call_llm_json
from co_scientist.platform.retrieval.article import Article
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.evidence_context import (
    PUBLIC_SNIPPET_CHARS,
    EvidenceCaps,
    _augment_evidence_context_with_meta_review,
    _probe_queries,
    _retrieve_probe_evidence,
    _retrieved_evidence_context,
    build_evidence_context,
)
from co_scientist.science.evidence_context import (
    merge_retrieved_articles as merge_retrieved_articles,
)
from co_scientist.science.prompts import get_deep_verification_prompt
from co_scientist.science.reflection.review_evidence import (
    with_researched as _with_researched,
)
from co_scientist.science.reflection.review_gate import VERIFICATION_QUERIES_KEY
from co_scientist.science.scheduling.funnel import finalists
from co_scientist.science.schemas.review import (
    DEEP_VERIFICATION_MAX_DECONTEXTUALIZATIONS,
    DEEP_VERIFICATION_MAX_SUB_ASSUMPTIONS,
)

logger = logging.getLogger(__name__)


_VALID_VERDICTS = frozenset({"holds", "weakened", "undermined"})


def has_valid_verification(result: Mapping[str, Any] | None) -> bool:
    verdict = result.get("verdict") if result is not None else None
    return isinstance(verdict, str) and verdict in _VALID_VERDICTS


async def verify_hypothesis(state: WorkflowState, hypothesis: Hypothesis) -> dict[str, Any] | None:
    """Durable items need call-local guards; event-loop primitives cannot be
    shared."""
    context = _VerificationContext(
        research_goal=state["research_goal"],
        model_name=state["model_name"],
        tool_registry=state.get("tool_registry"),
        state=state,
    )
    return await _verify_one(
        hypothesis,
        context,
        asyncio.Semaphore(1),
        _verification_evidence_context(state),
    )


# Checkpointed issuance prevents resumed runs from re-firing the verification
# wave.
VERIFICATION_MARKER = "deep_verification_issued"


# Bump for prompt/schema answer changes; moving the node alone does not change
# the verification inputs or require a new version.
DEEP_VERIFICATION_PROMPT_VERSION = 2


def _cited_evidence_identities(hypothesis: Hypothesis) -> list[str]:
    """Only cited sources affect this claim; unrelated new papers must not
    invalidate the whole leaderboard."""
    identities = set()
    for key, source in (hypothesis.citation_map or {}).items():
        source = source if isinstance(source, dict) else {}
        identity = (
            source.get("source_id")
            or source.get("doi")
            or source.get("url")
            or source.get("title")
            or key
        )
        identities.add(str(identity))
    return sorted(identities)


def verification_fingerprint(hypothesis: Hypothesis, model_name: str) -> str:
    """Reuse is valid only for the same claim, model, prompt version and
    cited evidence."""
    payload = json.dumps(
        {
            "prompt_version": DEEP_VERIFICATION_PROMPT_VERSION,
            "model": model_name,
            "text": " ".join((hypothesis.text or "").split()),
            "evidence": _cited_evidence_identities(hypothesis),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verification_issued(hypothesis: Hypothesis) -> bool:
    return bool(hypothesis.enrichments.get(VERIFICATION_MARKER))


def mark_verification_issued(hypothesis: Hypothesis) -> None:
    hypothesis.enrichments[VERIFICATION_MARKER] = True


def _needs_verification(hypothesis: Hypothesis, model_name: str) -> bool:
    if not hypothesis.is_rankable():
        return False
    if verification_issued(hypothesis):
        return False
    return hypothesis.deep_verification_fingerprint != (
        verification_fingerprint(hypothesis, model_name)
    )


def select_hypotheses_to_verify(
    hypotheses: list[Hypothesis],
    model_name: str,
) -> list[Hypothesis]:
    """Callers pass the finalists; issuance markers keep each verified once."""
    return [hypothesis for hypothesis in hypotheses if _needs_verification(hypothesis, model_name)]


_select_hypotheses_to_verify = select_hypotheses_to_verify


# Aggregate size bounds corpus breadth without reducing each source excerpt.
_MAX_VERIFICATION_CONTEXT_CHARS = 8 * PUBLIC_SNIPPET_CHARS


@dataclasses.dataclass(frozen=True)
class _VerificationContext:
    """Probe retrieval needs full search configuration and MCP state, not
    just scalars."""

    research_goal: str
    model_name: str
    tool_registry: Any | None
    state: WorkflowState


async def _call_verification(
    hypothesis: Hypothesis,
    context: _VerificationContext,
    evidence_context: str,
) -> dict[str, Any]:
    prompt, schema = get_deep_verification_prompt(
        research_goal=context.research_goal,
        hypothesis_text=hypothesis.text,
        tool_registry=context.tool_registry,
        evidence_context=evidence_context,
    )
    return await call_llm_json(
        prompt=prompt,
        spec=CompletionSpec(
            role="deep_verification",
            model_name=context.model_name,
            max_tokens=EXTENDED_MAX_TOKENS,
            temperature=LOW_TEMPERATURE,
            json_schema=schema,
        ),
    )


async def _verify_one(
    hypothesis: Hypothesis,
    context: _VerificationContext,
    semaphore: asyncio.Semaphore,
    evidence_context: str,
) -> dict[str, Any] | None:
    evidence_context = _augment_evidence_context_with_meta_review(evidence_context, context.state)
    return await _verify_within_semaphore(semaphore, hypothesis, context, evidence_context)


async def _verify_within_semaphore(
    semaphore: asyncio.Semaphore,
    hypothesis: Hypothesis,
    context: _VerificationContext,
    evidence_context: str,
) -> dict[str, Any] | None:
    """Failures become unverified without aborting peers; run-wide parking
    and spend errors propagate."""
    async with semaphore:
        try:
            return await _verify_with_probes(hypothesis, context, evidence_context)
        except TASK_CONTROL_FLOW_ERRORS:
            raise
        except Exception as e:
            logger.error("Deep verification failed: %s", e)
            return None


async def _verify_with_probes(
    hypothesis: Hypothesis,
    context: _VerificationContext,
    evidence_context: str,
) -> dict[str, Any]:
    """Reuse already-funded research. A finalist review's queries retrieve
    before the one call; without them, the probe retry buys one round."""
    queries = _review_queries(hypothesis)
    if queries:
        return await _verify_once(hypothesis, context, evidence_context, queries)
    initial = await _call_verification(hypothesis, context, evidence_context)
    queries = _probe_queries(initial)
    probed, retrieval_errors = await _retrieve_probe_evidence(context.state, queries)
    articles = _with_researched(context.state, hypothesis, probed)
    if not articles:
        initial["retrieval_queries"] = queries
        initial["retrieval_errors"] = retrieval_errors
        initial["retrieved_articles"] = []
        initial["verification_llm_calls"] = 1
        return initial
    targeted_context = _retrieved_evidence_context(articles)
    result = await _call_verification(
        hypothesis,
        context,
        f"{evidence_context}\n\nTargeted probe evidence:\n{targeted_context}",
    )
    result["retrieval_queries"] = queries
    result["retrieval_errors"] = retrieval_errors
    result["retrieved_articles"] = [article.to_dict() for article in articles]
    result["verification_llm_calls"] = 2
    return result


def _review_queries(hypothesis: Hypothesis) -> list[str]:
    queries = hypothesis.enrichments.get(VERIFICATION_QUERIES_KEY)
    if not isinstance(queries, list):
        return []
    return [str(query) for query in queries if str(query).strip()]


async def _verify_once(
    hypothesis: Hypothesis,
    context: _VerificationContext,
    evidence_context: str,
    queries: list[str],
) -> dict[str, Any]:
    probed, retrieval_errors = await _retrieve_probe_evidence(context.state, queries)
    articles = _with_researched(context.state, hypothesis, probed)
    if articles:
        evidence_context = (
            f"{evidence_context}\n\nTargeted evidence:\n{_retrieved_evidence_context(articles)}"
        )
    result = await _call_verification(hypothesis, context, evidence_context)
    result["retrieval_queries"] = queries
    result["retrieval_errors"] = retrieval_errors
    result["retrieved_articles"] = [article.to_dict() for article in articles]
    result["verification_llm_calls"] = 1
    return result


def _verification_evidence_context(state: WorkflowState) -> str:
    """Verification needs corpus breadth; bound total prompt size rather than
    voices."""
    context = build_evidence_context(
        state.get("articles"),
        private_sources=state.get("context_enrichment_sources"),
        caps=EvidenceCaps(total_chars=_MAX_VERIFICATION_CONTEXT_CHARS),
    )
    return context or "No retrieved evidence available."


# This is caller-stamped, not a schema verdict; unverified still ranks and
# publishes, while undermined demotes below sound ideas.
VERDICT_UNVERIFIED = "unverified"
_select_hypotheses_to_verify = select_hypotheses_to_verify


def mark_hypothesis_unverified(hypothesis: Hypothesis) -> None:
    """Failure is explicit and nonblocking; the issued attempt stays spent
    even though its fingerprint is stale."""
    hypothesis.deep_verification_probes = []
    hypothesis.deep_verification_verdict = VERDICT_UNVERIFIED
    hypothesis.enrichments.pop("deep_verification", None)


def _bounded_verification(result: dict[str, Any]) -> dict[str, Any]:
    """Providers may ignore schema caps; bound stored lists again to protect
    checkpoints."""
    bounded = dict(result)
    bounded["sub_assumptions"] = list(
        (result.get("sub_assumptions") or [])[:DEEP_VERIFICATION_MAX_SUB_ASSUMPTIONS]
    )
    bounded["decontextualizations"] = list(
        (result.get("decontextualizations") or [])[:DEEP_VERIFICATION_MAX_DECONTEXTUALIZATIONS]
    )
    return bounded


def _apply_verification_results(
    to_verify: list[Hypothesis],
    results: list[dict[str, Any] | None],
    model_name: str,
) -> tuple[int, int]:
    """Clear failed probes; recompute successful fingerprints after retrieval
    may change evidence."""
    verified_count = 0
    unverified_count = 0
    for hypothesis, result in zip(to_verify, results, strict=True):
        if not has_valid_verification(result):
            mark_hypothesis_unverified(hypothesis)
            unverified_count += 1
            continue
        assert result is not None
        hypothesis.deep_verification_probes = result.get("probes", [])
        hypothesis.deep_verification_verdict = result.get("verdict")
        hypothesis.enrichments["deep_verification"] = _bounded_verification(result)
        hypothesis.deep_verification_fingerprint = verification_fingerprint(hypothesis, model_name)
        verified_count += 1
    return verified_count, unverified_count


async def deep_verification_node(state: WorkflowState) -> dict[str, Any]:
    """Verify each finalist once; later cycles verify only new finalists."""
    hypotheses = state["hypotheses"]
    if not hypotheses:
        return {}

    to_verify = select_hypotheses_to_verify(finalists(state), state["model_name"])

    if not to_verify:
        logger.info("Deep verification: every finalist already verified, reusing")
        await emit_progress(
            state,
            "deep_verification_complete",
            "Reused deep verification for every finalist",
            PROGRESS_DEEP_VERIFICATION_COMPLETE,
        )
        return {}

    verified, unverified, llm_calls = await _run_verification_batch(state, to_verify)
    return _deep_verification_result(hypotheses, state["articles"], verified, unverified, llm_calls)


def _deep_verification_result(
    hypotheses: list[Hypothesis],
    articles: list[Article] | None,
    verified_count: int,
    unverified_count: int,
    llm_calls: int,
) -> dict[str, Any]:
    logger.info(
        "Deep verification complete: %s hypotheses, %s unverified",
        verified_count,
        unverified_count,
    )
    message = f"Deep-verified {verified_count} top hypotheses"
    if unverified_count:
        message += f"; {unverified_count} left explicitly unverified after a verification failure"
    metrics = create_metrics_update(deltas=MetricDeltas(llm_calls=llm_calls))
    return {
        "hypotheses": hypotheses,
        "articles": articles,
        "metrics": metrics,
        "messages": phase_message("deep_verification", message),
    }


async def _run_verification_batch(
    state: WorkflowState,
    to_verify: list[Hypothesis],
) -> tuple[int, int, int]:
    await emit_progress(
        state,
        "deep_verification_start",
        f"Deep-verifying {len(to_verify)} hypotheses...",
        PROGRESS_DEEP_VERIFICATION_START,
    )

    # Issuing spends the attempt even when verification fails.
    for hypothesis in to_verify:
        mark_verification_issued(hypothesis)

    tool_registry = state.get("tool_registry")
    evidence_context = _verification_evidence_context(state)
    results = await _gather_verification_results(to_verify, state, tool_registry, evidence_context)
    verified_count, unverified_count, llm_calls = _finalize_verification_batch(
        to_verify, results, state
    )

    await emit_progress(
        state,
        "deep_verification_complete",
        f"Deep-verified {verified_count} hypotheses",
        PROGRESS_DEEP_VERIFICATION_COMPLETE,
    )

    return verified_count, unverified_count, llm_calls


async def _gather_verification_results(
    to_verify: list[Hypothesis],
    state: WorkflowState,
    tool_registry: Any | None,
    evidence_context: str,
) -> list[dict[str, Any] | None]:
    """A fresh semaphore belongs to this batch and its event loop."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)
    context = _VerificationContext(
        research_goal=state["research_goal"],
        model_name=state["model_name"],
        tool_registry=tool_registry,
        state=state,
    )
    return await asyncio.gather(
        *[_verify_one(h, context, semaphore, evidence_context) for h in to_verify]
    )


def _finalize_verification_batch(
    to_verify: list[Hypothesis],
    results: list[dict[str, Any] | None],
    state: WorkflowState,
) -> tuple[int, int, int]:
    """An empty degraded result still spent its initial call and must be
    billed."""
    verified_count, unverified_count = _apply_verification_results(
        to_verify, results, state["model_name"]
    )
    state["articles"] = merge_retrieved_articles(state.get("articles"), results)
    llm_calls = sum(
        int(result.get("verification_llm_calls", 1)) for result in results if result is not None
    )
    return verified_count, unverified_count, llm_calls
