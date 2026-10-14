from __future__ import annotations

import logging
from typing import Any

from co_scientist.domains.research_state.claims.gate import ClaimEdge
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.domains.research_state.repository import records as store
from co_scientist.domains.safety.hypothesis import hypothesis_text, record_hypothesis_block
from co_scientist.domains.safety.hypothesis.safety import (
    is_blocking_status,
    review_hypothesis_safety,
)

logger = logging.getLogger(__name__)

EXCLUDED_HYPOTHESIS_STATUSES = frozenset({"rejected", "duplicate"})


def contradicted_hypothesis_ids(
    run_id: str,
    db_path: str | None,
    claim_edges: list[ClaimEdge] | None = None,
) -> set[str]:
    """Withhold categorical contradictions, but publish speculative proposals
    with their contradiction verdicts; match publication_gate.
    """
    if claim_edges is None:
        claim_edges = store.list_claim_edges(run_id, db_path=db_path)
    return {edge.hypothesis_id for edge in claim_edges if edge.is_categorical_contradiction}


def _supported_hypothesis_ids(edges: list[ClaimEdge]) -> set[str]:
    """Partial evidence clears Unverified; the badge and verified count must
    share this rule to remain complements.
    """
    return {edge.hypothesis_id for edge in edges if edge.is_supporting}


def unverified_hypothesis_ids(
    run_id: str,
    db_path: str | None,
    hyps: list[dict[str, Any]] | None = None,
) -> set[str]:
    """No claim edges means unassessed, not unsupported; the Unverified badge
    describes assessed ideas only.
    """
    edges = store.list_claim_edges(run_id, db_path=db_path)
    if not edges:
        return set()
    supported = _supported_hypothesis_ids(edges)
    rows = hyps if hyps is not None else hypotheses.list_hypotheses(run_id, db_path=db_path)
    all_hypothesis_ids = {str(hypothesis.get("id")) for hypothesis in rows}
    return all_hypothesis_ids - supported


def _verified_hypothesis_count(
    hyps: list[dict[str, Any]],
    claim_edges: list[ClaimEdge],
) -> int:
    """The verified tile complements Unverified using the same support rule;
    unassessed ideas count as neither.
    """
    if not claim_edges:
        return 0
    supported = _supported_hypothesis_ids(claim_edges)
    return sum(1 for hyp in hyps if str(hyp.get("id")) in supported)


def exclude_unsafe_hypotheses(
    run_id: str,
    hyps: list[dict[str, Any]],
    db_path: str | None,
    claim_edges: list[ClaimEdge] | None = None,
) -> list[dict[str, Any]]:
    """Honor persisted gate decisions; re-review and audit only legacy rows with
    no safety status.
    """
    contradicted = contradicted_hypothesis_ids(run_id, db_path, claim_edges)
    kept = [
        hyp for hyp in hyps if _hypothesis_passes_safety_gate(run_id, hyp, contradicted, db_path)
    ]
    _log_gate_outcome(kept, hyps, contradicted)
    return kept


def _log_gate_outcome(
    kept: list[dict[str, Any]],
    hyps: list[dict[str, Any]],
    contradicted: set[str],
) -> None:
    """Ordinary exclusions need no operator action; warn only when all ideas are
    excluded, with the actual cause breakdown.
    """
    total = len(hyps)
    if total and not kept:
        tally = _exclusion_tally(hyps, kept, contradicted)
        logger.warning(
            "Report gate: excluded all %s hypotheses from synthesis (%s); "
            "the report has no ideas to show.",
            total,
            _join_clauses(_exclusion_clauses(tally)),
        )
        return
    if len(kept) < total:
        logger.info(
            "Report gate: excluded %s of %s hypotheses from synthesis.",
            total - len(kept),
            total,
        )


_EXCLUSION_CAUSE_ORDER = (
    "review_rejected",
    "duplicate",
    "contradicted",
    "safety",
)

# Keep rejection, duplication and safety holds distinct; reviewer unsafe
# judgments differ from the safety pipeline.
_EXCLUSION_CAUSE_PHRASES = {
    "review_rejected": (
        "rejected by peer review before ranking (the reviewer judged them "
        "inaccurate, non-novel, unsafe, or evidence-blocked)"
    ),
    "duplicate": "folded into a higher-ranked idea that says the same thing",
    "contradicted": "contradicted by the evidence",
    "safety": "withheld by the safety review",
}


def _exclusion_cause(hyp: dict[str, Any], contradicted: set[str]) -> str:
    """Match release-gate precedence so summary causes agree with why each idea
    was withheld.
    """
    status = hyp.get("status")
    if status == "duplicate":
        return "duplicate"
    if status == "rejected":
        return "review_rejected"
    if str(hyp.get("id")) in contradicted:
        return "contradicted"
    return "safety"


def _exclusion_tally(
    hyps: list[dict[str, Any]],
    kept: list[dict[str, Any]],
    contradicted: set[str],
) -> dict[str, int]:
    kept_ids = {str(hyp.get("id")) for hyp in kept}
    tally = dict.fromkeys(_EXCLUSION_CAUSE_ORDER, 0)
    for hyp in hyps:
        if str(hyp.get("id")) in kept_ids:
            continue
        tally[_exclusion_cause(hyp, contradicted)] += 1
    return tally


def _exclusion_clauses(tally: dict[str, int]) -> list[str]:
    clauses = []
    for cause in _EXCLUSION_CAUSE_ORDER:
        count = tally.get(cause, 0)
        if not count:
            continue
        verb = "was" if count == 1 else "were"
        clauses.append(f"{count} {verb} {_EXCLUSION_CAUSE_PHRASES[cause]}")
    return clauses


def _join_clauses(clauses: list[str]) -> str:
    if len(clauses) <= 1:
        return clauses[0] if clauses else ""
    if len(clauses) == 2:
        return " and ".join(clauses)
    return ", ".join(clauses[:-1]) + ", and " + clauses[-1]


def _empty_leaderboard_reason(idea_count: int, tally: dict[str, int]) -> str:
    """Derive the reason from actual gate causes; zero generated ideas is
    different from withholding every idea.
    """
    if not idea_count:
        return "No hypothesis could be published: the run produced no ideas."
    clauses = _exclusion_clauses(tally)
    if not clauses:
        return "No hypothesis could be published: every idea was withheld from the ranked report."
    total = sum(tally.values())
    noun = "idea" if total == 1 else "ideas"
    return f"No hypothesis could be published: of {total} {noun}, {_join_clauses(clauses)}."


def _hypothesis_passes_safety_gate(
    run_id: str,
    hyp: dict[str, Any],
    contradicted: set[str],
    db_path: str | None,
) -> bool:
    if hyp.get("status") in EXCLUDED_HYPOTHESIS_STATUSES:
        return False
    if str(hyp.get("id")) in contradicted:
        logger.info(
            "Excluding hypothesis %s from synthesis: contradicted claim",
            hyp.get("id"),
        )
        return False
    status = hyp.get("safety_status")
    # Honor the persisted screen decision to avoid duplicate reviews and audit
    # rows.
    if status and status != "pending":
        if is_blocking_status(str(status)):
            logger.info(
                "Excluding hypothesis %s from synthesis: %s",
                hyp.get("id"),
                status,
            )
            return False
        return True
    return _legacy_hypothesis_passes_safety_gate(run_id, hyp, db_path)


def _legacy_hypothesis_passes_safety_gate(
    run_id: str, hyp: dict[str, Any], db_path: str | None
) -> bool:
    review = review_hypothesis_safety(hypothesis_text(hyp))
    if not review.blocks_tournament:
        return True
    record_hypothesis_block(run_id, hyp.get("id"), review, db_path=db_path)
    logger.info(
        "Excluding hypothesis %s from synthesis: %s",
        hyp.get("id"),
        review.outcome.value,
    )
    return False
