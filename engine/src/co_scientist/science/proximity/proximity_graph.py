from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Mapping
from typing import Any

from co_scientist.domains.research_state.proximity_edges import (
    PROXIMITY_METHOD,
    PROXIMITY_METHOD_VERSION,
)
from co_scientist.domains.research_state.text_utils import token_coverage

# Round before admission so the stored similarity and floor decision agree, and
# per-edge JSON cannot grow with full floating-point representations.
_SIMILARITY_DECIMALS = 3


def pair_similarity(text_a: str, text_b: str) -> float:
    """Harmonic bidirectional coverage is Dice similarity, symmetric across
    lengths; Jaccard would dilute containment."""
    coverage_a = token_coverage(text_a, text_b)
    coverage_b = token_coverage(text_b, text_a)
    total = coverage_a + coverage_b
    if total == 0.0:
        return 0.0
    return round(2 * coverage_a * coverage_b / total, _SIMILARITY_DECIMALS)


# The literature leaves the metric unspecified; these fixed weights are the
# local numeric projection of qualitative model judgment.
_DEGREE_WEIGHT: dict[str, float] = {
    "high": 1.0,
    "medium": 0.6,
    "low": 0.3,
}


# Computed edges need distinct provenance and degree labels so readers cannot
# mistake token measurements for qualitative model judgments.
PROXIMITY_COMPUTED_METHOD = "token-dice"

PROXIMITY_COMPUTED_DEGREE = "computed"


# Pair count grows quadratically; the edge floor bounds checkpoint JSON and
# persistent drain inserts on the single SQLite writer.
PROXIMITY_EDGE_FLOOR = 0.25


# Use the same indexed/normalized-text resolution as dedup; a re-quote must not
# name different ideas in clustering and the graph.
_MATCH_PREFIX_CHARS = 100


def member_match_key(text: str) -> str:
    """Dedup and graph must normalize echoed members identically; the fixed
    prefix tolerates re-quoted tails, case and padding."""
    return text[:_MATCH_PREFIX_CHARS].strip().lower()


@dataclasses.dataclass(frozen=True)
class SurvivorIndex:
    """Only survivors resolve with fixed original indices; require full texts
    so missing context cannot silently erase computed edges."""

    by_index: dict[int, str]
    by_text: dict[str, str]
    texts: Mapping[str, str]


def _degree_weight(degree: str | None) -> float:
    return _DEGREE_WEIGHT.get((degree or "low").lower(), _DEGREE_WEIGHT["low"])


def _resolve_member_id(member: dict[str, Any], survivors: SurvivorIndex) -> str | None:
    """Indices are the live schema contract; text-only resolution would empty
    the graph on responses that correctly omit echoed text."""
    index = member.get("index")
    if isinstance(index, int):
        resolved = survivors.by_index.get(index)
        if resolved is not None:
            return resolved
    text = member.get("text")
    if isinstance(text, str) and text:
        return survivors.by_text.get(member_match_key(text))
    return None


def _cluster_member_ids(cluster: dict[str, Any], survivors: SurvivorIndex) -> list[tuple[str, str]]:
    members: list[tuple[str, str]] = []
    for member in cluster.get("similar_hypotheses", []):
        hyp_id = _resolve_member_id(member, survivors)
        if hyp_id is not None:
            members.append((hyp_id, member.get("similarity_degree", "low")))
    return members


def _accumulate_cluster_edges(
    edges: dict[frozenset[str], dict[str, Any]],
    cluster: dict[str, Any],
    survivors: SurvivorIndex,
) -> None:
    cluster_id = cluster.get("cluster_id", "unknown")
    members = _cluster_member_ids(cluster, survivors)
    for (id_a, deg_a), (id_b, deg_b) in itertools.combinations(members, 2):
        if id_a == id_b:
            continue
        key = frozenset({id_a, id_b})
        weight_a, weight_b = _degree_weight(deg_a), _degree_weight(deg_b)
        stronger = deg_a if weight_a >= weight_b else deg_b
        weight = max(weight_a, weight_b)
        existing = edges.get(key)
        if existing is None or weight > existing["similarity"]:
            edges[key] = {
                "source": id_a,
                "target": id_b,
                "similarity": weight,
                "degree": stronger,
                "cluster_id": cluster_id,
                "method": PROXIMITY_METHOD,
            }


def _node_ids(survivors: SurvivorIndex) -> set[str]:
    return set(survivors.by_index.values()) | set(survivors.by_text.values())


def _computed_edges(
    judged: dict[frozenset[str], dict[str, Any]],
    node_ids: set[str],
    texts: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Sorted IDs preserve deterministic pair order; absent text is
    unmeasured, not evidence of zero similarity."""
    edges: list[dict[str, Any]] = []
    for id_a, id_b in itertools.combinations(sorted(node_ids), 2):
        if frozenset({id_a, id_b}) in judged:
            continue
        similarity = pair_similarity(texts.get(id_a, ""), texts.get(id_b, ""))
        if similarity < PROXIMITY_EDGE_FLOOR:
            continue
        edges.append(
            {
                "source": id_a,
                "target": id_b,
                "similarity": similarity,
                "degree": PROXIMITY_COMPUTED_DEGREE,
                "cluster_id": None,
                "method": PROXIMITY_COMPUTED_METHOD,
            }
        )
    return edges


def _proximity_graph_meta(
    edges: dict[frozenset[str], dict[str, Any]],
    computed: list[dict[str, Any]],
    research_goal: str,
    model: str,
    updated_at: float,
) -> dict[str, Any]:
    """Legacy node/edge counts describe judged edges; report computed edges
    separately to retain that persisted meaning."""
    return {
        "method": PROXIMITY_METHOD,
        "version": PROXIMITY_METHOD_VERSION,
        "model": model,
        "research_goal": research_goal,
        "updated_at": updated_at,
        "node_count": len(
            {v["source"] for v in edges.values()} | {v["target"] for v in edges.values()}
        ),
        "edge_count": len(edges),
        "computed_edge_count": len(computed),
        "computed_method": PROXIMITY_COMPUTED_METHOD,
        "edge_floor": PROXIMITY_EDGE_FLOOR,
    }


def build_proximity_graph(
    similarity_clusters: list[dict[str, Any]],
    survivors: SurvivorIndex,
    *,
    research_goal: str,
    model: str,
    updated_at: float,
) -> dict[str, Any]:
    judged: dict[frozenset[str], dict[str, Any]] = {}
    for cluster in similarity_clusters:
        _accumulate_cluster_edges(judged, cluster, survivors)
    computed = _computed_edges(judged, _node_ids(survivors), survivors.texts)

    return {
        "edges": list(judged.values()) + computed,
        "meta": _proximity_graph_meta(judged, computed, research_goal, model, updated_at),
    }
