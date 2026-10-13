"""Candidate indices avoid echoing abstracts in relevance output.
Retrieval versions must keep their persisted meaning when scoring changes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from co_scientist.core.constants import DEFAULT_MAX_TOKENS, HIGH_TEMPERATURE
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.platform.llm import CompletionSpec, call_llm_json

logger = logging.getLogger(__name__)

# Science registers the judgment prompt and schema at import; retrieval sits
# below science and cannot import them.
_judgment_prompt: Callable[..., str] | None = None
_judgment_schema: dict[str, Any] | None = None


def register_judgment_prompt(build: Callable[..., str], schema: dict[str, Any]) -> None:
    global _judgment_prompt, _judgment_schema
    _judgment_prompt, _judgment_schema = build, schema


RETRIEVAL_METHOD = "hybrid-lexical-semantic"
RETRIEVAL_METHOD_VERSION = "2"
_LEXICAL_ONLY_VERSION = f"lexical-only/{RETRIEVAL_METHOD_VERSION}"
_HYBRID_VERSION = f"{RETRIEVAL_METHOD}/{RETRIEVAL_METHOD_VERSION}"

_LEXICAL_WEIGHT = 0.5
_SEMANTIC_WEIGHT = 0.5


# Bound semantic cost by the evidence budget and an absolute candidate cap.
_SEMANTIC_POOL_MULTIPLIER = 3
_SEMANTIC_POOL_CAP = 24


# Larger batches trade fewer calls for longer prompts and larger failure scope.
_RELEVANCE_BATCH_SIZE = 10


# Bound each candidate so long abstracts cannot dominate a batch prompt.
_ABSTRACT_CHAR_BUDGET = 1500


def combine_hybrid_score(lexical_raw: float, semantic: float | None) -> float:
    # Fusion already normalizes to [0, 1]; this clamp only removes floating-point noise.
    lexical = max(0.0, min(1.0, lexical_raw))
    if semantic is None:
        return round(lexical, 4)
    bounded_semantic = max(0.0, min(1.0, semantic))
    combined = _LEXICAL_WEIGHT * lexical + _SEMANTIC_WEIGHT * bounded_semantic
    return round(combined, 4)


def _semantic_pool_size(candidate_count: int, budget: int) -> int:
    if budget <= 0:
        return 0
    cap = min(budget * _SEMANTIC_POOL_MULTIPLIER, _SEMANTIC_POOL_CAP)
    return min(candidate_count, cap)


_FAILED_JUDGMENT_RATIONALE = "semantic relevance scoring failed"


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _build_candidates_block(pool_ids: list[str], ranked: dict[str, dict[str, Any]]) -> str:
    """The prompt's 1-based indices join judgments back to candidates."""
    lines = []
    for number, paper_id in enumerate(pool_ids, start=1):
        metadata = ranked[paper_id]
        title = str(metadata.get("title") or "Unknown")
        abstract = str(metadata.get("abstract") or "")[:_ABSTRACT_CHAR_BUDGET]
        lines.append(f"**Candidate {number}:**\nTitle: {title}\nAbstract: {abstract}")
    return "\n\n".join(lines)


def _match_batch_judgments(judgments: list[Any], pool_ids: list[str]) -> list[Any]:
    """Malformed indices fill unclaimed slots in order; short offline responses
    must degrade missing candidates rather than abort the search."""
    count = len(pool_ids)
    slots: list[Any] = [None] * count
    unplaced: list[Any] = []
    claimed: set[int] = set()
    for entry in judgments:
        number = entry.get("index") if isinstance(entry, dict) else None
        if (
            isinstance(number, int)
            and not isinstance(number, bool)
            and 1 <= number <= count
            and number not in claimed
        ):
            slots[number - 1] = entry
            claimed.add(number)
        else:
            unplaced.append(entry)

    remaining = iter(unplaced)
    for index in range(count):
        if slots[index] is None:
            slots[index] = next(remaining, None)
    return slots


def _score_matched_judgments(
    pool_ids: list[str], judgments: list[Any]
) -> list[tuple[str, float, str]]:
    matched = _match_batch_judgments(judgments, pool_ids)
    scored = []
    for paper_id, entry in zip(pool_ids, matched, strict=True):
        scored.append((paper_id, *_one_judgment(entry)))
    return scored


def _one_judgment(entry: Any) -> tuple[float, str]:
    """json_object downgrade can bypass schema enforcement; bad types must
    degrade only their candidate rather than abort sibling batches."""
    if not isinstance(entry, dict):
        return 0.0, _FAILED_JUDGMENT_RATIONALE
    try:
        relevance = float(entry.get("relevance", 0.0))
    except (TypeError, ValueError):
        return 0.0, _FAILED_JUDGMENT_RATIONALE
    return relevance, str(entry.get("rationale") or "")


async def _judge_batch(
    pool_ids: list[str],
    ranked: dict[str, dict[str, Any]],
    research_goal: str,
    model_name: str,
) -> list[tuple[str, float, str]]:
    """A failed call degrades only its batch; sibling batches must still
    finish."""
    if _judgment_prompt is None or _judgment_schema is None:
        raise RuntimeError("no relevance judgment prompt is registered")
    prompt = _judgment_prompt(
        research_goal=research_goal,
        candidates_block=_build_candidates_block(pool_ids, ranked),
    )
    try:
        result = await call_llm_json(
            prompt=prompt,
            spec=CompletionSpec(
                role="relevance",
                model_name=model_name,
                max_tokens=DEFAULT_MAX_TOKENS,
                temperature=HIGH_TEMPERATURE,
                json_schema=_judgment_schema,
            ),
        )
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        logger.warning(
            "Semantic relevance batch scoring failed for %s candidates: %s",
            len(pool_ids),
            exc,
        )
        return [(paper_id, 0.0, _FAILED_JUDGMENT_RATIONALE) for paper_id in pool_ids]

    return _score_matched_judgments(pool_ids, result.get("judgments") or [])


def _stamp_hybrid_score(
    metadata: dict[str, Any],
    lexical_raw: float,
    semantic: float | None,
    rationale: str,
) -> None:
    """Stamping overwrites retrieval_score; rereading it as lexical input
    would double-weight semantic relevance."""
    metadata["retrieval_score"] = combine_hybrid_score(lexical_raw, semantic)
    metadata["retrieval_rationale"] = rationale
    metadata["retriever_version"] = (
        _HYBRID_VERSION if semantic is not None else _LEXICAL_ONLY_VERSION
    )


def _lexical_raw_scores(
    ranked: dict[str, dict[str, Any]],
) -> dict[str, float]:
    """Capture raw scores before any stamping overwrites retrieval_score."""
    return {pid: float(metadata.get("retrieval_score") or 0.0) for pid, metadata in ranked.items()}


def _stamp_lexical_baseline(
    ranked: dict[str, dict[str, Any]], lexical_raw: dict[str, float]
) -> None:
    for paper_id, metadata in ranked.items():
        _stamp_hybrid_score(metadata, lexical_raw[paper_id], None, "")


def _semantic_pool_ids(
    ranked: dict[str, dict[str, Any]], research_goal: str, budget: int
) -> list[str]:
    if not research_goal:
        return []
    pool_size = _semantic_pool_size(len(ranked), budget)
    return list(ranked)[:pool_size] if pool_size > 0 else []


def _resort_by_hybrid_score(
    ranked: dict[str, dict[str, Any]], scored_ids: set[str]
) -> dict[str, dict[str, Any]]:
    """Unscored candidates retain lexical order after the semantically ranked
    pool."""
    scored = sorted(
        (pid for pid in ranked if pid in scored_ids),
        key=lambda pid: -float(ranked[pid]["retrieval_score"]),
    )
    rest = [pid for pid in ranked if pid not in scored_ids]
    return {pid: ranked[pid] for pid in (*scored, *rest)}


async def apply_semantic_relevance(
    ranked: dict[str, dict[str, Any]],
    research_goal: str,
    model_name: str,
    budget: int,
) -> dict[str, dict[str, Any]]:
    if not ranked:
        return ranked
    lexical_raw = _lexical_raw_scores(ranked)
    _stamp_lexical_baseline(ranked, lexical_raw)

    pool_ids = _semantic_pool_ids(ranked, research_goal, budget)
    if not pool_ids:
        return ranked

    batches = await asyncio.gather(
        *(
            _judge_batch(batch_ids, ranked, research_goal, model_name)
            for batch_ids in _chunked(pool_ids, _RELEVANCE_BATCH_SIZE)
        )
    )
    for batch in batches:
        for paper_id, relevance, rationale in batch:
            _stamp_hybrid_score(ranked[paper_id], lexical_raw[paper_id], relevance, rationale)

    return _resort_by_hybrid_score(ranked, set(pool_ids))
