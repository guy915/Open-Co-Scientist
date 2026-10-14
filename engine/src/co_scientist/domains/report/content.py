from __future__ import annotations

import logging
from typing import Any

from co_scientist.domains.research_state.claims.chunking import parent_evidence_id
from co_scientist.domains.research_state.claims.gate import ClaimEdge, EntailmentLabel
from co_scientist.domains.research_state.entity_names import (
    extract_entity_names,
)
from co_scientist.domains.research_state.text_utils import (
    hypothesis_statement,
    hypothesis_title,
    readable_experiment_summary,
)
from co_scientist.domains.safety.hypothesis.safety import is_blocking_status

logger = logging.getLogger(__name__)


def _claim_evidence_ids(edge: ClaimEdge, span_key: str = "supporting") -> list[str]:
    """Provenance lives on spans, not edges; map chunk passage ids to parent
    article ids for reader references.
    """
    ids: list[str] = []
    for span in edge.row.get(span_key) or []:
        raw = span.get("evidence_id") if isinstance(span, dict) else None
        if raw:
            ids.append(parent_evidence_id(str(raw)))
    return ids


def _knowledge_base_topics(
    hypotheses: list[dict[str, Any]],
    claim_edges: list[ClaimEdge],
) -> list[dict[str, Any]]:
    references_by_hypothesis: dict[str, list[str]] = {}
    for edge in claim_edges:
        if edge.is_supporting:
            references_by_hypothesis.setdefault(edge.hypothesis_id, []).extend(
                _claim_evidence_ids(edge)
            )
    topics: list[dict[str, Any]] = []
    for hypothesis in hypotheses[:8]:
        hypothesis_id = str(hypothesis.get("id") or "")
        topics.append(
            {
                "id": f"topic-{hypothesis_id}",
                "title": str(hypothesis.get("title") or "Mechanistic finding"),
                "summary": str(hypothesis.get("mechanism") or hypothesis.get("statement") or ""),
                "detail": (
                    readable_experiment_summary(str(hypothesis.get("experimental_context") or ""))
                    or str(hypothesis.get("expected_effect") or "")
                ),
                "reference_ids": sorted(set(references_by_hypothesis.get(hypothesis_id, []))),
            }
        )
    return topics


def _topic_reference_ids(raw: dict[str, Any], evidence_id_by_title: dict[str, str]) -> list[str]:
    references = raw.get("references") or []
    return sorted(
        {
            evidence_id_by_title.get(str(ref.get("title") or ""), "")
            for ref in references
            if isinstance(ref, dict)
        }
        - {""}
    )


def _synthesized_topic_from_raw(
    raw: Any, evidence_id_by_title: dict[str, str], fallback_index: int
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    reference_ids = _topic_reference_ids(raw, evidence_id_by_title)
    if not reference_ids:
        return None
    return {
        "id": str(raw.get("id") or f"topic-{fallback_index}"),
        "theme": str(raw.get("theme") or ""),
        "title": str(raw.get("title") or "Technical topic"),
        "summary": str(raw.get("summary") or ""),
        "detail": str(raw.get("detail") or ""),
        "uncertainty": str(raw.get("uncertainty") or ""),
        "reference_ids": reference_ids,
    }


def _synthesized_knowledge_base_topics(
    research_overview: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(research_overview, dict):
        return []
    raw_topics = research_overview.get("knowledge_base")
    if not isinstance(raw_topics, list):
        return []
    evidence_id_by_title = {
        str(item.get("title") or ""): str(item.get("id") or "") for item in evidence
    }
    topics: list[dict[str, Any]] = []
    for raw in raw_topics:
        topic = _synthesized_topic_from_raw(raw, evidence_id_by_title, len(topics) + 1)
        if topic is not None:
            topics.append(topic)
    return topics


# Contradiction notes must match the release gate: categorical facts withhold;
# speculative proposals remain published.
_WITHHELD_CONTRADICTION_NOTE = (
    "Contradicted by the evidence, so the idea proposing it was withheld from the ranked report."
)
_PROPOSAL_CONTRADICTION_NOTE = (
    "Contradicted by the evidence; this is the idea's own proposal, so the "
    "contradiction alone does not remove the idea from the report."
)


def _contradicted_claims(claim_edges: list[ClaimEdge]) -> list[str]:
    """Keep findings from withheld ideas visible: categorical contradictions
    explain withholding, while proposal contradictions stay published. Omit
    textless entries to avoid empty sections.
    """
    claims: list[str] = []
    for edge in claim_edges:
        claim = edge.claim.strip()
        if edge.is_contradicting and claim:
            note = (
                _WITHHELD_CONTRADICTION_NOTE
                if edge.is_categorical_contradiction
                else _PROPOSAL_CONTRADICTION_NOTE
            )
            claims.append(f"{claim} ({note})")
    return claims


def _recommended_direction(raw: Any) -> dict[str, Any]:
    """Schema-ignoring providers can return bare recommendation strings; avoid
    printing object representations.
    """
    if not isinstance(raw, dict):
        return {
            "focus_area": "",
            "recommendation": raw,
            "justification": "",
        }
    return {
        "focus_area": raw.get("focus_area") or "",
        "recommendation": raw.get("recommendation") or "",
        "justification": raw.get("justification") or "",
    }


def _key_findings(hypotheses: list[dict[str, Any]]) -> list[str]:
    """Use the same statement formatter as markdown so one report cannot name an
    idea two ways; omit blank proposal labels.
    """
    statements = (hypothesis_statement(h) for h in hypotheses[:5])
    return [f"Proposed hypothesis: {statement}" for statement in statements if statement]


def _agent_insights(
    hypotheses: list[dict[str, Any]],
    claim_edges: list[ClaimEdge],
    meta_review: dict[str, Any] | None,
) -> dict[str, Any]:
    """Read the whole claim graph so withheld ideas do not lose the
    contradictions that explain their absence.
    """
    meta = meta_review or {}
    return {
        "key_findings": _key_findings(hypotheses),
        "uncertainties": [str(item) for item in meta.get("common_weaknesses", [])],
        "contradictions": _contradicted_claims(claim_edges),
        "recommended_directions": [
            _recommended_direction(item) for item in meta.get("strategic_recommendations", [])
        ],
        "next_experiments": [
            summary
            for hypothesis in hypotheses[:5]
            if (
                summary := readable_experiment_summary(
                    str(hypothesis.get("experimental_context") or "")
                )
            )
        ],
    }


def _claim_edge_reasons(claim_edges: list[ClaimEdge]) -> dict[str, set[str]]:
    """Partial evidence counts as support; an unsupported reason would
    contradict the cleared Unverified badge.
    """
    edge_reasons: dict[str, set[str]] = {}
    for edge in claim_edges:
        if edge.is_supporting or edge.is_excused:
            continue
        edge_reasons.setdefault(edge.hypothesis_id, set()).add(
            "Evidence verification did not support every material claim."
        )
    return edge_reasons


def _high_potential_bucket(
    safe_hypotheses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The two buckets partition the entire pool, so their counts must sum to
    the idea count. Use shared report titles.
    """
    return [
        {
            "id": str(hypothesis.get("id")),
            "title": hypothesis_title(hypothesis),
            "reason": ("Released by safety and evidence gates and ranked by Elo."),
        }
        for hypothesis in safe_hypotheses
    ]


def _non_viable_reasons(hypothesis: dict[str, Any], edge_reasons: dict[str, set[str]]) -> list[str]:
    """Report causes in release-gate order; distinguish review rejection from
    deduplication to avoid false reader claims.
    """
    if hypothesis.get("status") == "duplicate":
        return ["Folded into a higher-ranked idea that makes the same proposal."]
    if hypothesis.get("status") == "rejected":
        return [
            "Set aside during review: the reviewer judged it scientifically"
            " unsound or already established (a reviewer's own judgment,"
            " not a literature search)."
        ]
    # Unsupported and weakly scored ideas still rank and publish; only explicit
    # release-gate exclusions leave this bucket.
    hypothesis_id = str(hypothesis.get("id"))
    reasons = sorted(edge_reasons.get(hypothesis_id, set()))
    if is_blocking_status(str(hypothesis.get("safety_status") or "")):
        reasons.append("The scientific safety review blocked this idea.")
    return reasons


def _non_viable_bucket(
    all_hypotheses: list[dict[str, Any]],
    safe_ids: set[str],
    edge_reasons: dict[str, set[str]],
) -> list[dict[str, Any]]:
    non_viable = []
    for hypothesis in all_hypotheses:
        hypothesis_id = str(hypothesis.get("id"))
        if hypothesis_id in safe_ids:
            continue
        reasons = _non_viable_reasons(hypothesis, edge_reasons)
        non_viable.append(
            {
                "id": hypothesis_id,
                "title": hypothesis_title(hypothesis),
                "reason": " ".join(reasons) or "Withheld from the ranked report.",
            }
        )
    return non_viable


def _idea_buckets(
    safe_hypotheses: list[dict[str, Any]],
    all_hypotheses: list[dict[str, Any]],
    claim_edges: list[ClaimEdge],
) -> dict[str, list[dict[str, Any]]]:
    safe_ids = {str(hypothesis.get("id")) for hypothesis in safe_hypotheses}
    edge_reasons = _claim_edge_reasons(claim_edges)
    return {
        "high_potential": _high_potential_bucket(safe_hypotheses),
        "non_viable": _non_viable_bucket(all_hypotheses, safe_ids, edge_reasons),
    }


def _enrich_claim_span(raw_span: Any, sources: dict[str, dict[str, Any]]) -> Any:
    if not isinstance(raw_span, dict):
        return raw_span
    span = dict(raw_span)
    source = sources.get(parent_evidence_id(str(span.get("evidence_id") or "")), {})
    span["source_title"] = str(span.get("source_title") or source.get("title") or "")
    span["source"] = str(span.get("source") or source.get("source") or "")
    span["url"] = str(span.get("url") or source.get("url") or "")
    return span


def _enrich_claim_edge(edge: ClaimEdge, sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    enriched = dict(edge.row)
    for key in ("supporting", "contradicting"):
        enriched[key] = [_enrich_claim_span(raw, sources) for raw in edge.row.get(key) or []]
    return enriched


def released_claim_evidence(
    hypotheses: list[dict[str, Any]],
    claim_edges: list[ClaimEdge],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    released_ids = {str(hypothesis.get("id") or "") for hypothesis in hypotheses}
    sources = {str(item.get("id") or ""): item for item in evidence if item.get("id")}
    return [
        _enrich_claim_edge(edge, sources)
        for edge in claim_edges
        if edge.hypothesis_id in released_ids
    ]


# Each settled label reads its own span list; partial support is not a
# knowledge fact.
_FACT_KINDS: dict[EntailmentLabel | None, tuple[str, str]] = {
    EntailmentLabel.SUPPORTS: ("fact", "supporting"),
    EntailmentLabel.CONTRADICTS: ("contradiction", "contradicting"),
}

# Claim statements name both drivers and targets, so allow more entities
# than the hypothesis-title extractor normally expects.
_MAX_ENTITIES_PER_FACT = 5


def _fact_row(edge: ClaimEdge) -> dict[str, Any] | None:
    statement = edge.claim.strip()
    if edge.label not in _FACT_KINDS or not statement:
        return None
    kind, span_key = _FACT_KINDS[edge.label]
    evidence_ids = _claim_evidence_ids(edge, span_key)
    return {
        "hypothesis_id": edge.hypothesis_id,
        "evidence_id": evidence_ids[0] if evidence_ids else None,
        "kind": kind,
        "statement": statement,
        "entities": extract_entity_names(statement, max_entities=_MAX_ENTITIES_PER_FACT),
        "state": str(edge.row["label"]),
    }


def derive_knowledge_facts(claim_edges: list[ClaimEdge]) -> list[dict[str, Any]]:
    """Record settled findings across the whole claim graph, independent of
    report release filtering.
    """
    rows = (_fact_row(edge) for edge in claim_edges)
    return [row for row in rows if row is not None]
