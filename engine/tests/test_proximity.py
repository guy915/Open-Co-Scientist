from __future__ import annotations

from typing import Any

import pytest

from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.research_state.proximity_edges import PROXIMITY_METHOD
from co_scientist.science.proximity import (
    proximity,
    proximity_node,
)
from co_scientist.science.proximity.proximity_graph import SurvivorIndex, build_proximity_graph
from tests._state import make_hypothesis, make_state

# Legacy text-echo fixtures exercise fallback; live responses use indices.
_HIGH_HIGH_CLUSTERS: dict[str, Any] = {
    "similarity_clusters": [
        {
            "cluster_id": "c1",
            "similar_hypotheses": [
                {
                    "text": "alpha pathway drives tumor growth",
                    "similarity_degree": "high",
                },
                {
                    "text": "beta pathway drives tumor growth",
                    "similarity_degree": "high",
                },
            ],
        }
    ]
}


# Live schemas name members by index and forbid echoed text.
_HIGH_HIGH_BY_INDEX: dict[str, Any] = {
    "similarity_clusters": [
        {
            "cluster_id": "c1",
            "similar_hypotheses": [
                {"index": 0, "similarity_degree": "high"},
                {"index": 1, "similarity_degree": "high"},
            ],
        }
    ]
}


def _alpha_beta_pair() -> tuple[Hypothesis, Hypothesis]:
    low = make_hypothesis(text="alpha pathway drives tumor growth", elo_rating=1200)
    high = make_hypothesis(text="beta pathway drives tumor growth", elo_rating=1400)
    return low, high


def _stub_clusters(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> None:

    async def fake(**_: Any) -> dict[str, Any]:
        return response

    monkeypatch.setattr(proximity, "call_llm_json", fake)


@pytest.mark.parametrize("clusters", [_HIGH_HIGH_BY_INDEX, _HIGH_HIGH_CLUSTERS])
async def test_high_similarity_duplicate_removed_keeping_best_elo(
    monkeypatch: pytest.MonkeyPatch, clusters: dict[str, Any]
) -> None:
    low, high = _alpha_beta_pair()
    state = make_state(hypotheses=[low, high])
    _stub_clusters(monkeypatch, clusters)
    result = await proximity_node(state)
    assert len(result["hypotheses"]) == 1
    assert result["hypotheses"][0].elo_rating == 1400
    assert len(result["removed_duplicates"]) == 1
    assert result["removed_duplicates"][0]["elo_rating"] == 1200
    archived = result["removed_duplicates"][0]
    assert archived["hypothesis"]["id"]
    assert archived["hypothesis"]["text"] == archived["text"]
    assert archived["kept_hypothesis_id"]


def _disjoint_texts(*ids: str) -> dict[str, str]:
    """Disjoint vocabulary suppresses computed edges, isolating judged
    clusters."""
    return {hyp_id: " ".join(f"{hyp_id}word{n}" for n in range(6)) for hyp_id in ids}


def _survivors(*ids: str, texts: dict[str, str] | None = None) -> SurvivorIndex:
    """An empty text fallback prevents broken index resolution from passing."""
    return SurvivorIndex(
        by_index=dict(enumerate(ids)),
        by_text={},
        texts=texts or _disjoint_texts(*ids),
    )


def _clusters() -> list[dict[str, object]]:
    return [
        {
            "cluster_id": "c1",
            "similar_hypotheses": [
                {"index": 0, "similarity_degree": "high"},
                {"index": 1, "similarity_degree": "medium"},
            ],
        },
        {
            "cluster_id": "c2",
            "similar_hypotheses": [
                {"index": 2, "similarity_degree": "low"},
            ],
        },
    ]


_SURVIVORS = _survivors("h-a", "h-b", "h-c")


def test_weighted_edges_stay_within_clusters_and_record_provenance() -> None:
    graph = build_proximity_graph(
        _clusters(),
        _SURVIVORS,
        research_goal="my goal",
        model="fake/model",
        updated_at=999.0,
    )
    [edge] = graph["edges"]
    assert {edge["source"], edge["target"]} == {"h-a", "h-b"}
    assert (edge["similarity"], edge["degree"], edge["cluster_id"]) == (
        1.0,
        "high",
        "c1",
    )
    meta = graph["meta"]
    assert meta["method"] == PROXIMITY_METHOD
    assert (meta["model"], meta["research_goal"]) == ("fake/model", "my goal")
    assert (meta["edge_count"], meta["node_count"]) == (1, 2)
