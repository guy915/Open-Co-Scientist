from __future__ import annotations

from typing import Any, NamedTuple

from co_scientist.domains.report.content import (
    _agent_insights,
    _idea_buckets,
    _knowledge_base_topics,
    _synthesized_knowledge_base_topics,
    derive_knowledge_facts,
    released_claim_evidence,
)
from co_scientist.domains.report.gates import (
    _exclusion_tally,
    _verified_hypothesis_count,
    contradicted_hypothesis_ids,
    exclude_unsafe_hypotheses,
)
from co_scientist.domains.report.markdown import ReportMarkdownInputs, render_report_markdown
from co_scientist.domains.report.retrieval import failed_sources_in
from co_scientist.domains.research_state.claims.gate import ClaimEdge
from co_scientist.domains.research_state.elo import live_leaderboard, rank_for_publication
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.domains.research_state.repository import records as store
from co_scientist.platform.db import retrieval_calls as retrieval


class ReportRequest(NamedTuple):
    """Database terms can require notice of invoked skills. Scientist setup and
    synthesized evaluation fields have distinct meanings.
    """

    research_goal: str
    run_mode: str
    provider: str
    citation_summary: dict[str, int] | None = None
    meta_review: dict[str, Any] | None = None
    research_overview: dict[str, Any] | None = None
    degraded_sections: list[str] | None = None
    retrieval_degradation: dict[str, Any] | None = None
    skills_used: dict[str, int] | None = None
    execution_time: float | None = None
    summary: str | None = None
    setup: dict[str, Any] | None = None
    attributes: list[dict[str, Any]] | None = None
    critical_criteria: list[Any] | None = None
    prepared_at: float | None = None
    goal_restatement: str | None = None
    db_path: str | None = None


class _ReportData(NamedTuple):
    hyps: list[dict[str, Any]]
    all_hyps: list[dict[str, Any]]
    claim_edges: list[ClaimEdge]
    released_claim_edges: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    # Match counts and displayed rows must describe the same persisted set.
    matches: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    retrieval_calls: list[dict[str, Any]]
    # Reuse gate inputs: its legacy fallback can write an audit row.
    exclusion_tally: dict[str, int]


class _BuiltReport(NamedTuple):
    payload: dict[str, Any]
    markdown: str
    facts: list[dict[str, Any]]
    exclusion_tally: dict[str, int]


async def build_report_content(run_id: str, req: ReportRequest) -> _BuiltReport:
    """Counts come from drained store rows; resolve topics once for JSON and
    markdown to prevent divergent views.
    """
    data = _gather_report_data(run_id, req.db_path)
    knowledge_base = _resolve_knowledge_base(data, req)
    inputs = ReportMarkdownInputs(
        research_goal=req.research_goal,
        goal_restatement=req.goal_restatement,
        provider=req.provider,
        top_hypotheses=[hyp for hyp in data.hyps if _examined(hyp, data.claim_edges)],
        screened_hypotheses=[hyp for hyp in data.hyps if not _examined(hyp, data.claim_edges)],
        meta_review=req.meta_review,
        citation_summary=req.citation_summary,
        research_overview=req.research_overview,
        knowledge_base=knowledge_base,
        setup=req.setup,
        attributes=req.attributes,
        critical_criteria=req.critical_criteria,
        prepared_at=req.prepared_at,
        summary=req.summary,
        claim_evidence=data.released_claim_edges,
        skills_used=req.skills_used,
        retrieval_calls=data.retrieval_calls,
        hypothesis_title_by_id=_hypothesis_title_by_id(data.hyps),
        matches=data.matches,
        citations=data.citations,
        evidence=data.evidence,
        reviews=data.reviews,
    )
    return _BuiltReport(
        payload=_assemble_report_payload(data, req, knowledge_base),
        markdown=render_report_markdown(inputs),
        # The knowledge base records findings from the whole claim graph,
        # including withheld ideas.
        facts=derive_knowledge_facts(data.claim_edges),
        exclusion_tally=data.exclusion_tally,
    )


def _hypothesis_title_by_id(hyps: list[dict[str, Any]]) -> dict[str, str]:
    """Overview examples can name ideas outside the markdown top-five slice, so
    resolve titles against the full released pool.
    """
    return {str(h["id"]): str(h["title"]) for h in hyps if h.get("id") and h.get("title")}


def _resolve_knowledge_base(data: _ReportData, req: ReportRequest) -> list[dict[str, Any]]:
    """Share one resolved topic list between payload and markdown; use released
    claim-graph topics only without an overview.
    """
    synthesized = _synthesized_knowledge_base_topics(req.research_overview, data.evidence)
    return synthesized or _knowledge_base_topics(data.hyps, data.claim_edges)


def _examined(hyp: dict[str, Any], claim_edges: list[ClaimEdge]) -> bool:
    """Finalists carry a deep-verification verdict; older runs may carry
    only claim checks. Anything else was screened, not examined."""
    if hyp.get("verification_verdict"):
        return True
    return any(edge.hypothesis_id == hyp.get("id") for edge in claim_edges)


def _gather_report_data(run_id: str, db_path: str | None) -> _ReportData:
    all_hyps = hypotheses.list_hypotheses(run_id, db_path=db_path)
    claim_edges = store.list_claim_edges(run_id, db_path=db_path)
    # Order once after safety filtering so top ideas and standings open with the
    # same idea.
    hyps = rank_for_publication(exclude_unsafe_hypotheses(run_id, all_hyps, db_path, claim_edges))
    evidence = store.list_evidence(run_id, db_path=db_path)
    released_claim_edges = released_claim_evidence(hyps, claim_edges, evidence)
    contradicted = contradicted_hypothesis_ids(run_id, db_path, claim_edges)
    return _ReportData(
        hyps=hyps,
        all_hyps=all_hyps,
        claim_edges=claim_edges,
        released_claim_edges=released_claim_edges,
        evidence=evidence,
        citations=store.list_citations(run_id, db_path=db_path),
        matches=store.list_matches(run_id, db_path=db_path),
        reviews=store.list_reviews(run_id, db_path=db_path),
        retrieval_calls=retrieval.list_retrieval_calls(run_id, db_path=db_path),
        exclusion_tally=_exclusion_tally(all_hyps, hyps, contradicted),
    )


def _assemble_report_payload(
    data: _ReportData,
    req: ReportRequest,
    knowledge_base: list[dict[str, Any]],
) -> dict[str, Any]:
    hyps, all_hyps, claim_edges = data.hyps, data.all_hyps, data.claim_edges
    payload: dict[str, Any] = {
        "research_goal": req.research_goal,
        "run_mode": req.run_mode,
        "provider": req.provider,
        "leaderboard": live_leaderboard(hyps),
        "hypothesis_count": len(hyps),
        "idea_count": len(all_hyps),
        "verified_count": _verified_hypothesis_count(hyps, claim_edges),
        "evidence_count": len(data.evidence),
        "match_count": len(data.matches),
        "citation_summary": req.citation_summary or {},
        "meta_review": req.meta_review or {},
        "research_overview": req.research_overview or {},
        "knowledge_base": knowledge_base,
        "agent_insights": _agent_insights(hyps, claim_edges, req.meta_review),
        "idea_buckets": _idea_buckets(hyps, all_hyps, claim_edges),
        "claim_evidence": data.released_claim_edges,
        "degraded_sections": list(req.degraded_sections or []),
        "retrieval_degradation": req.retrieval_degradation,
        "failed_sources": failed_sources_in(data.reviews, data.retrieval_calls),
        "skills_used": dict(req.skills_used or {}),
        "reviews": list(data.reviews),
    }
    if req.execution_time is not None:
        payload["execution_time"] = req.execution_time
    return payload
