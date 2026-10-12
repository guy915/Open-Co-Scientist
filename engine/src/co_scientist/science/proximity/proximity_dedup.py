import logging
from typing import Any

from co_scientist.domains.research_state.models import Hypothesis, rank_by_elo
from co_scientist.science.proximity.proximity_graph import member_match_key

logger = logging.getLogger(__name__)


def _match_cluster_member(
    similar_hyp: dict[str, Any],
    hypotheses: list[Hypothesis],
    by_prefix: dict[str, Hypothesis],
) -> Hypothesis | None:
    index = similar_hyp.get("index")
    if isinstance(index, int) and 0 <= index < len(hypotheses):
        return hypotheses[index]
    text = similar_hyp.get("text")
    if isinstance(text, str) and text:
        return by_prefix.get(member_match_key(text))
    return None


def _apply_cluster_membership(
    matched: Hypothesis, cluster_id: str, similar_hyp: dict[str, Any]
) -> None:
    """Overlapping model clusters use first-match degree rather than later
    overwrite."""
    matched.similarity_cluster_id = cluster_id
    if matched.similarity_degree is None:
        matched.similarity_degree = similar_hyp.get("similarity_degree", "low")


def _assign_cluster_ids(
    hypotheses: list[Hypothesis], similarity_clusters: list[dict[str, Any]]
) -> None:
    """Indices keep output bounded; text is a legacy fallback using the same
    normalized match key as the graph, so both identify re-quotes alike."""
    # First matching text key wins, retaining list-order resolution.
    by_prefix: dict[str, Hypothesis] = {}
    for hyp in hypotheses:
        by_prefix.setdefault(member_match_key(hyp.text), hyp)

    for cluster in similarity_clusters:
        cluster_id = cluster.get("cluster_id", "unknown")
        for similar_hyp in cluster.get("similar_hypotheses", []):
            matched = _match_cluster_member(similar_hyp, hypotheses, by_prefix)
            if matched is None:
                continue
            _apply_cluster_membership(matched, cluster_id, similar_hyp)


def _partition_by_similarity_degree(
    cluster_hypotheses: list[Hypothesis],
) -> tuple[list[Hypothesis], list[Hypothesis]]:
    """Medium and low similarity are relationships, not permission to delete."""
    high_similarity = [h for h in cluster_hypotheses if h.similarity_degree == "high"]
    others = [h for h in cluster_hypotheses if h.similarity_degree != "high"]
    return high_similarity, others


def _build_removed_duplicate_record(
    duplicate: Hypothesis, cluster_id: str, kept: Hypothesis
) -> dict[str, Any]:
    return {
        "text": duplicate.text,
        "cluster_id": cluster_id,
        "reason": "high_similarity_duplicate",
        "kept_hypothesis_id": kept.id,
        "kept_instead": kept.text[:200],
        "elo_rating": duplicate.elo_rating,
        "score": duplicate.score,
        # Archive the full immutable idea for lineage/history without
        # reactivating it.
        "hypothesis": duplicate.to_dict(),
    }


def _build_removed_duplicates_for_cluster(
    high_similarity: list[Hypothesis], cluster_id: str, best: Hypothesis
) -> list[dict[str, Any]]:
    """Drops are archived and summarized already; debug traces avoid one
    SQLite/log-window row per successful deduplication."""
    removed_duplicates: list[dict[str, Any]] = []
    for duplicate in high_similarity[1:]:
        removed_duplicates.append(_build_removed_duplicate_record(duplicate, cluster_id, best))
        logger.debug(
            "removed duplicate from cluster %s: %s... (elo %s)",
            cluster_id,
            duplicate.text[:100],
            duplicate.elo_rating,
        )
    return removed_duplicates


def _resolve_cluster_duplicates(
    cluster_id: str, cluster_hypotheses: list[Hypothesis]
) -> tuple[list[Hypothesis], list[dict[str, Any]]]:
    """False high similarity silently deletes distinct ideas. Only high may
    delete; medium and low remain relations."""
    high_similarity, others = _partition_by_similarity_degree(cluster_hypotheses)
    hypotheses_to_keep: list[Hypothesis] = list(others)
    if not high_similarity:
        return hypotheses_to_keep, []

    high_similarity = rank_by_elo(high_similarity)
    best = high_similarity[0]
    hypotheses_to_keep.append(best)

    removed_duplicates = _build_removed_duplicates_for_cluster(high_similarity, cluster_id, best)

    return hypotheses_to_keep, removed_duplicates


def _group_by_cluster(
    hypotheses: list[Hypothesis],
) -> dict[str, list[Hypothesis]]:
    """Group from assigned IDs so unclustered ideas are still accounted for
    once."""
    clusters_dict: dict[str, list[Hypothesis]] = {}
    for hyp in hypotheses:
        cluster_id = hyp.similarity_cluster_id or "unclustered"
        clusters_dict.setdefault(cluster_id, []).append(hyp)
    return clusters_dict


def _dedupe_by_cluster(
    hypotheses: list[Hypothesis],
) -> tuple[list[Hypothesis], list[dict[str, Any]]]:
    removed_duplicates: list[dict[str, Any]] = []
    hypotheses_to_keep: list[Hypothesis] = []

    clusters_dict = _group_by_cluster(hypotheses)

    for cluster_id, cluster_hypotheses in clusters_dict.items():
        kept, removed = _resolve_cluster_duplicates(cluster_id, cluster_hypotheses)
        hypotheses_to_keep.extend(kept)
        removed_duplicates.extend(removed)

    return hypotheses_to_keep, removed_duplicates
