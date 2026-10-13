from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

import co_scientist.api.contracts as contracts
from co_scientist.api.contracts.reports import Report
from co_scientist.api.runs.lifecycle import adjudicate_safety
from co_scientist.api.runs.support import _require_run, _run_or_404
from co_scientist.core.async_bridge import off_loop
from co_scientist.domains.chat.repository.example_summaries import get_display_report
from co_scientist.domains.report import unverified_hypothesis_ids
from co_scientist.domains.research_state.publication import rank_for_publication
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.domains.research_state.repository import records as store
from co_scientist.platform.db import runs

router = APIRouter()


@router.get("/{run_id}/hypotheses", response_model=contracts.HypothesesResponse)
@off_loop
def get_hypotheses(run_id: str) -> dict[str, Any]:
    """Return the run's hypotheses with Elo state, lineage, and verification."""
    run = _run_or_404(run_id)
    hyps = rank_for_publication(hypotheses.list_hypotheses(run_id))
    # Without a deep-verification verdict or claim checks an idea was screened,
    # not examined, so "Unverified" would misstate why.
    assessed = {edge.hypothesis_id for edge in store.list_claim_edges(run_id)}
    for hyp in hyps:
        hyp["screened"] = not hyp.get("verification_verdict") and hyp.get("id") not in assessed
    # Offline badges describe persisted run provenance, never the current
    # process
    # backend.
    if runs.run_used_offline(run):
        for hyp in hyps:
            hyp["unverified"] = False
    else:
        unverified = unverified_hypothesis_ids(run_id, None, hyps)
        for hyp in hyps:
            hyp["unverified"] = str(hyp.get("id")) in unverified and not hyp["screened"]
    return {"hypotheses": hyps}


@router.get("/{run_id}/evidence", response_model=contracts.EvidenceResponse)
@off_loop
def get_evidence(run_id: str) -> dict[str, Any]:
    """Return the literature evidence retrieved for the run."""
    _require_run(run_id)
    return {"evidence": store.list_evidence(run_id)}


@router.get("/{run_id}/matches", response_model=contracts.MatchesResponse)
@off_loop
def get_matches(run_id: str) -> dict[str, Any]:
    """Return the run's tournament matches with Elo snapshots."""
    _require_run(run_id)
    return {"matches": store.list_matches(run_id)}


@router.get("/{run_id}/reviews", response_model=contracts.ReviewsResponse)
@off_loop
def get_reviews(run_id: str) -> dict[str, Any]:
    """Return reviewer and meta-review notes for the run."""
    _require_run(run_id)
    return {"reviews": store.list_reviews(run_id)}


@router.get("/{run_id}/safety", response_model=contracts.SafetyResponse)
@off_loop
def get_safety(run_id: str) -> dict[str, Any]:
    """Return the run's intake/final safety-gate decisions."""
    _require_run(run_id)
    return {"safety": store.list_safety_decisions(run_id)}


# Literal routes precede dynamic IDs because the router uses first-match
# ordering.
router.post("/{run_id}/safety/{decision_id}/adjudicate")(adjudicate_safety)


@router.get("/{run_id}/citations")
@off_loop
def get_citations(run_id: str) -> dict[str, Any]:
    """Return the run's citation rows with classification states."""
    _require_run(run_id)
    return {"citations": store.list_citations(run_id)}


@router.get("/{run_id}/claim-evidence", response_model=contracts.ClaimsResponse)
@off_loop
def get_claim_evidence(run_id: str) -> dict[str, Any]:
    """Return the run's claim-level entailment graph (Milestone 5).

    Each edge is one atomic claim of a hypothesis with its assessed label
    (an ``EntailmentLabel`` value; ``co_scientist.domains.research_state.claims.gate``
    says what each means) and the exact supporting/contradicting passages that drove the
    verdict.
    """
    _require_run(run_id)
    return {"claim_evidence": store.list_claim_evidence(run_id)}


@router.get("/{run_id}/report", response_model=Report)
@off_loop
def get_report(run_id: str) -> dict[str, Any]:
    """Return the latest structured report, or 404 before synthesis."""
    _require_run(run_id)
    report = get_display_report(run_id)
    if not report:
        raise HTTPException(status_code=404, detail="no report yet")
    return report


@router.get("/{run_id}/report.md", response_class=PlainTextResponse)
@off_loop
def get_report_markdown(run_id: str) -> PlainTextResponse:
    """Return the rendered Goal Report document as a file download."""
    _require_run(run_id)
    report = get_display_report(run_id)
    md = report["markdown_text"] if report else None
    if md is None:
        raise HTTPException(status_code=404, detail="no report yet")
    return PlainTextResponse(
        md,
        headers={
            "Content-Disposition": f'attachment; filename="{run_id}.md"',
        },
    )
