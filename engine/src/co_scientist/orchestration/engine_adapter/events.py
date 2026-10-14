from __future__ import annotations

from collections.abc import Callable
from typing import Any

from co_scientist.domains.chat.repository import messages as store
from co_scientist.domains.chat.repository.messages import NewMessage
from co_scientist.domains.research_state.elo import rank_for_publication
from co_scientist.domains.research_state.proximity_edges import is_judged_edge
from co_scientist.orchestration.run_events import hypothesis_stub


def _canonical_event_type(node_name: str) -> str:
    return "supervisor.plan" if node_name == "supervisor" else node_name


# Derive advertised stages from actual engine nodes so diagnostics cannot name
# nonexistent stages.
_ENGINE_PIPELINE_AGENTS: list[str] = [
    "supervisor",
    "literature_review",
    "generate",
    "reflection",
    "review",
    "ranking",
    "proximity",
    "evolve",
    "meta_review",
    "deep_verification",
    "research_overview",
]


def _proximity_payload_extra(state: dict[str, Any]) -> dict[str, Any]:
    """Unjudged deterministic edges do not define clusters; counting them
    would invent an unknown mega-cluster.
    """
    graph: dict[str, Any] = state.get("proximity_graph") or {}
    members: dict[str, set[str]] = {}
    for edge in graph.get("edges") or []:
        if not is_judged_edge(edge):
            continue
        cluster_id = str(edge.get("cluster_id") or "unknown")
        bucket = members.setdefault(cluster_id, set())
        for side in ("source", "target"):
            node_id = edge.get(side)
            if node_id:
                bucket.add(str(node_id))
    return {"clusters": {cid: len(ids) for cid, ids in members.items()}}


# Meta-review state has no explicit leaderboard; derive its slice in the one
# order every reader-facing surface uses.
_META_REVIEW_TOP_K = 5


def _meta_review_payload_extra(state: dict[str, Any]) -> dict[str, Any]:
    meta_review: dict[str, Any] = state.get("meta_review") or {}
    hyps: list[dict[str, Any]] = state.get("hypotheses") or []
    ranked = rank_for_publication(hyps)
    top_k_ids = [str(h["id"]) for h in ranked[:_META_REVIEW_TOP_K] if h.get("id")]
    return {
        "critique": str(meta_review.get("summary", "")),
        "top_k_ids": top_k_ids,
    }


def _deep_verification_payload_extra(state: dict[str, Any]) -> dict[str, Any]:
    hyps: list[dict[str, Any]] = state.get("hypotheses") or []
    probes = [
        {
            "hypothesis_id": h.get("id"),
            "verdict": h.get("deep_verification_verdict"),
            "probes": h.get("deep_verification_probes"),
        }
        for h in hyps
        if h.get("deep_verification_probes")
    ]
    return {"verified": len(probes), "probes": probes}


_PAYLOAD_BUILDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "generate": lambda state: {
        "count": len(state.get("hypotheses") or []),
        "hypotheses": [hypothesis_stub(h) for h in state.get("hypotheses") or []],
    },
    "literature_review": lambda state: {
        "count": len(state.get("articles") or []),
        "evidence": [
            {
                "title": str(a.get("title") or "Untitled"),
                "url": str(a.get("url") or ""),
            }
            for a in state.get("articles") or []
        ],
    },
    "ranking": lambda state: {
        "matches": [
            {"winner": str(m.get("winner") or "")} for m in state.get("tournament_matchups") or []
        ]
    },
    "evolve": lambda state: {
        "children": [
            hypothesis_stub(h) for h in state.get("hypotheses") or [] if h.get("evolution_history")
        ]
    },
    "reflection": lambda state: {
        "reviewed": sum(1 for h in state.get("hypotheses") or [] if h.get("reviews"))
    },
    "proximity": _proximity_payload_extra,
    "meta_review": _meta_review_payload_extra,
    "deep_verification": _deep_verification_payload_extra,
    "research_overview": lambda state: {"research_overview": state.get("research_overview") or {}},
    "supervisor.plan": lambda _: {"agents": list(_ENGINE_PIPELINE_AGENTS)},
}


def _canonical_engine_payload(
    node_name: str, node_type: str, state: dict[str, Any]
) -> dict[str, Any]:
    """Project minimal JSON-safe stubs: raw engine objects can contain
    embeddings that event serialization cannot encode.
    """
    payload: dict[str, Any] = {
        "node": node_name,
        "iteration": state.get("current_iteration", 0),
    }
    builder = _PAYLOAD_BUILDERS.get(node_type)
    if builder is not None:
        payload.update(builder(state))
    # Expose schema degradation from its first committed occurrence, rather than
    # only in the final report.
    degraded = [str(name) for name in state.get("degraded_nodes") or []]
    if degraded:
        payload["degraded"] = degraded
    # Unavailable retrieval bypasses nodes entirely; carry the loss explicitly
    # so watchers can distinguish missing work.
    retrieval = state.get("retrieval_degradation")
    if isinstance(retrieval, dict) and retrieval:
        payload["retrieval_degraded"] = retrieval
    return payload


def _milestone_generate(payload: dict[str, Any]) -> str:
    count = payload.get("count", 0)
    itr = payload.get("iteration", 0)
    label = f"iteration {itr}" if itr else "initial"
    return f"{count} hypotheses generated ({label})"


_MILESTONE_BUILDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "supervisor.plan": lambda _: "Research plan ready — supervisor complete",
    "generate": _milestone_generate,
    "ranking": lambda p: (
        f"Tournament complete (iteration {p.get('iteration', 0)}, "
        f"{len(p.get('matches') or [])} matches)"
    ),
    "meta_review": lambda _: "Meta-review complete",
    "evolve": lambda p: (
        f"{len(p.get('children') or [])} hypotheses evolved (iteration {p.get('iteration', 0)})"
    ),
    "reflection": lambda p: f"{p.get('reviewed', 0)} hypotheses reviewed",
    "proximity": lambda p: f"{len(p.get('clusters') or {})} clusters identified",
    "deep_verification": lambda p: f"{p.get('verified', 0)} hypotheses verified",
    "research_overview": lambda _: "Research overview ready",
}


def _format_milestone(node_type: str, payload: dict[str, Any]) -> str | None:
    builder = _MILESTONE_BUILDERS.get(node_type)
    return builder(payload) if builder is not None else None


def append_node_milestone(
    run_id: str,
    node_type: str,
    payload: dict[str, Any],
    *,
    db_path: str | None = None,
) -> None:
    milestone = _format_milestone(node_type, payload)
    if milestone:
        store.append_message(
            NewMessage(
                run_id=run_id,
                sender="system",
                content=milestone,
                kind="milestone",
            ),
            db_path=db_path,
        )
