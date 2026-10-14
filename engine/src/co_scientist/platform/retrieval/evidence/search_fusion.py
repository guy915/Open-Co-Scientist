import logging
from typing import TYPE_CHECKING, Any

from co_scientist.platform.retrieval.evidence.article_support import (
    _metadata_is_retracted,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import SearchSourceConfig


def _fill_reserved_slots(
    ranked: dict[str, dict[str, Any]],
    source_map: dict[str, str],
    sources: list["SearchSourceConfig"],
    budget: int,
) -> list[str]:
    reserved: list[str] = []
    for source in sources:
        if source.reserved_slots <= 0:
            continue
        remaining = budget - len(reserved)
        if remaining <= 0:
            break
        from_source = [paper_id for paper_id in ranked if source_map.get(paper_id) == source.tool]
        reserved.extend(from_source[: min(source.reserved_slots, remaining)])
    return reserved


def _fill_remaining_by_score(
    ranked: dict[str, dict[str, Any]],
    reserved: list[str],
    budget: int,
) -> list[str]:
    selected = list(reserved)
    reserved_ids = set(reserved)
    for paper_id in ranked:
        if len(selected) >= budget:
            break
        if paper_id not in reserved_ids:
            selected.append(paper_id)
    return selected


def _exclude_retracted(
    ranked: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Demotion still admits retractions in thin pools or reserved slots;
    exclude them before either admission path."""
    return {
        paper_id: metadata
        for paper_id, metadata in ranked.items()
        if not _metadata_is_retracted(metadata)
    }


def select_within_budget(
    ranked: dict[str, dict[str, Any]],
    source_map: dict[str, str],
    sources: list["SearchSourceConfig"],
    budget: int,
) -> list[str]:
    """Reservations protect source diversity without inflating global weights
    enough to displace other sources."""
    if budget <= 0:
        return []

    admissible = _exclude_retracted(ranked)
    reserved = _fill_reserved_slots(admissible, source_map, sources, budget)
    selected = _fill_remaining_by_score(admissible, reserved, budget)

    if reserved:
        logger.info(
            "Evidence budget %s: %s reserved, %s by score",
            budget,
            len(reserved),
            len(selected) - len(reserved),
        )
    return selected


# RRF uses source rank because raw scores are incomparable; k=2 follows
# qdrant position_score (Apache-2.0; Cormack, Clarke & Buettcher 2009).
_RRF_K = 2


# Weights encode editorial trust; unknown sources share the unreviewed floor.
_SOURCE_RRF_WEIGHTS = {
    "pubmed": 3.0,
    "openalex": 2.0,
    "europepmc": 1.5,
    "literature": 1.5,
    "preprints": 1.0,
    "arxiv": 1.0,
    "biorxiv": 1.0,
    "web": 1.0,
}
_DEFAULT_SOURCE_RRF_WEIGHT = 1.0


def _source_rrf_weight(metadata: dict[str, Any]) -> float:
    source = str(metadata.get("source") or metadata.get("_source_name") or "").lower()
    return _SOURCE_RRF_WEIGHTS.get(source, _DEFAULT_SOURCE_RRF_WEIGHT)


def _rrf_position_score(position: int, weight: float) -> float:
    if weight <= 0:
        return 0.0
    return 1.0 / ((position + 1) / weight + _RRF_K - 1)


def _normalize_rrf_pool(raw_scores: dict[str, float], retracted_ids: set[str]) -> dict[str, float]:
    """Retracted scores must not skew the live pool's range; RRF has no fixed
    raw scale."""
    live = {pid: score for pid, score in raw_scores.items() if pid not in retracted_ids}
    if not live:
        return dict.fromkeys(raw_scores, 0.0)
    lo, hi = min(live.values()), max(live.values())
    span = hi - lo
    normalized: dict[str, float] = {}
    for paper_id, score in raw_scores.items():
        if paper_id in retracted_ids:
            normalized[paper_id] = 0.0
        elif span > 0:
            normalized[paper_id] = round((score - lo) / span, 4)
        else:
            normalized[paper_id] = 1.0
    return normalized


def _rank_search_results(
    metadata: dict[str, dict[str, Any]],
    raw_scores: dict[str, float],
) -> dict[str, dict[str, Any]]:
    retracted_ids = {
        paper_id for paper_id, item in metadata.items() if _metadata_is_retracted(item)
    }
    normalized = _normalize_rrf_pool(raw_scores, retracted_ids)
    for paper_id, item in metadata.items():
        item["retrieval_score"] = normalized[paper_id]
        item["correction_status"] = "retracted" if paper_id in retracted_ids else "current"
    return dict(
        sorted(
            metadata.items(),
            key=lambda pair: (
                pair[0] in retracted_ids,
                -float(pair[1]["retrieval_score"]),
                str(pair[1].get("title") or "").lower().strip(),
                pair[0],
            ),
        )
    )


def _duplicate_owner_id(
    paper_id: str,
    metadata: Any,
    deduplicate: bool,
    seen_titles: dict[str, str],
) -> str | None:
    """Sources use different IDs for one paper; title joins them. Preserve
    first-owner metadata/provenance while adding duplicate ranks."""
    if not deduplicate or not isinstance(metadata, dict):
        return None
    title = str(metadata.get("title") or "").lower().strip()
    if not title:
        return None
    owner_id = seen_titles.get(title)
    if owner_id is not None:
        logger.debug("Duplicate title, folding into %s: %s...", owner_id, title[:60])
        return owner_id
    seen_titles[title] = paper_id
    return None


def merge_search_results(
    source_results: list[tuple[str, dict[str, dict[str, Any]]]],
    deduplicate: bool = True,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    all_paper_metadata: dict[str, dict[str, Any]] = {}
    paper_source_map: dict[str, str] = {}
    seen_titles: dict[str, str] = {}

    raw_rrf_scores: dict[str, float] = {}

    for source_tool_id, results in source_results:
        for position, (paper_id, metadata) in enumerate(results.items()):
            owner_id = _duplicate_owner_id(paper_id, metadata, deduplicate, seen_titles)
            if owner_id is None:
                all_paper_metadata[paper_id] = metadata

                # Later retrieval must retain the first owner's source-tool
                # config.
                paper_source_map[paper_id] = source_tool_id
            weight = _source_rrf_weight(metadata)
            target_id = owner_id or paper_id
            raw_rrf_scores[target_id] = raw_rrf_scores.get(target_id, 0.0) + _rrf_position_score(
                position, weight
            )

    ranked = _rank_search_results(all_paper_metadata, raw_rrf_scores)
    ranked_source_map = {paper_id: paper_source_map[paper_id] for paper_id in ranked}
    return ranked, ranked_source_map
