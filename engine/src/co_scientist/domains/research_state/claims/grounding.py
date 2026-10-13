from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from co_scientist.domains.research_state.claims import (
    _ASSESSOR_DETERMINISTIC,
    Assessor,
    BatchAssessor,
    EvidencePassage,
    GateDecision,
    GateResult,
    assess_claim,
    assess_claims_batch,
    deterministic_assessor,
    extract_atomic_claims,
    publication_gate,
    retrieve_passages,
)
from co_scientist.domains.research_state.claims.assessor import _DEFAULT_RETRIEVAL_TOP_K
from co_scientist.domains.research_state.claims.gate import (
    ClaimAssessment,
    ClaimRole,
    EntailmentLabel,
    SupportSpan,
)
from co_scientist.domains.research_state.repository import records as store
from co_scientist.domains.research_state.repository.records import (
    NewClaimEvidence,
    NewSafetyDecision,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ClaimRecord:
    claim: str
    role: str


def _passage_identity(passage: Any) -> dict[str, str]:
    """The gate names a passage by its article and finalize by its store row,
    so a verdict is keyed by what was read; `_rebased` re-points its spans."""
    return {
        "text": str(passage.text),
        "source": str(passage.source),
        "url": str(passage.url),
    }


def claim_fingerprint(
    record: ClaimRecord,
    passages: Sequence[Any],
    assessor_id: str,
    top_k: int = _DEFAULT_RETRIEVAL_TOP_K,
) -> str:
    """Reuse depends on the actual retrieved inputs, not an approximate
    fingerprint of the entire evidence pool.
    """
    payload = {
        "assessor": assessor_id,
        "claim": record.claim,
        "role": record.role,
        "evidence": [
            _passage_identity(passage)
            for passage in retrieve_passages(record.claim, passages, top_k=top_k)
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def combined_fingerprint(claim_fingerprints: Sequence[str]) -> str:
    """Previously computed input digests avoid repeating retrieval solely to
    fingerprint it.
    """
    return hashlib.sha256(
        json.dumps(list(claim_fingerprints), separators=(",", ":")).encode()
    ).hexdigest()


def _spans_from(payload: Mapping[str, Any], key: str) -> tuple[SupportSpan, ...]:
    return tuple(
        SupportSpan(
            evidence_id=str(span.get("evidence_id") or ""),
            quote=str(span.get("quote") or ""),
            start=int(span.get("start") or 0),
            end=int(span.get("end") or 0),
            source=str(span.get("source") or ""),
            url=str(span.get("url") or ""),
        )
        for span in payload.get(key) or ()
        if isinstance(span, Mapping)
    )


def _restore_one(record: Mapping[str, Any], assessor_id: str) -> ClaimAssessment | None:
    claim = str(record.get("claim") or "")
    label = str(record.get("label") or "")
    if not claim or label not in {item.value for item in EntailmentLabel}:
        return None
    return ClaimAssessment(
        claim=claim,
        label=EntailmentLabel(label),
        supporting_passages=_spans_from(record, "supporting_passages"),
        contradicting_passages=_spans_from(record, "contradicting_passages"),
        assessor=assessor_id,
        verification_method=str(record.get("verification_method") or "legacy_unknown"),
    )


def reusable_assessments(
    gate_record: Mapping[str, Any] | None,
) -> dict[str, ClaimAssessment]:
    """Legacy assessments without input fingerprints cannot safely claim
    unchanged provenance.
    """
    if not gate_record:
        return {}
    assessor_id = str(gate_record.get("assessor") or "")
    reusable: dict[str, ClaimAssessment] = {}
    for record in gate_record.get("claims") or ():
        if not isinstance(record, Mapping):
            continue
        fingerprint = str(record.get("fingerprint") or "")
        restored = _restore_one(record, assessor_id) if fingerprint else None
        if restored is not None:
            reusable[fingerprint] = restored
    return reusable


ASSESSMENT_CONCURRENCY = 12

_CLAIM_FIELD_ROLES = (
    ("statement", ClaimRole.SPECULATIVE.value),
    ("mechanism", ClaimRole.CATEGORICAL.value),
    ("expected_effect", ClaimRole.SPECULATIVE.value),
)


@dataclasses.dataclass(frozen=True)
class AssessorSpec:
    assessor: Assessor = deterministic_assessor
    assessor_id: str = _ASSESSOR_DETERMINISTIC
    batch_assessor: BatchAssessor | None = None


def _claim_records(hyp: Mapping[str, Any]) -> list[tuple[str, str]]:
    roles: dict[str, str] = {}
    ordered: list[str] = []
    for field, role in _CLAIM_FIELD_ROLES:
        for claim in extract_atomic_claims(str(hyp.get(field) or "")):
            if claim not in roles:
                ordered.append(claim)
                roles[claim] = role
            elif role == ClaimRole.CATEGORICAL:
                roles[claim] = role
    return [(claim, roles[claim]) for claim in ordered]


def assess_claim_groups(
    groups: Sequence[Sequence[str]],
    passages: Sequence[EvidencePassage],
    spec: AssessorSpec,
    *,
    parallel: bool = True,
) -> list[list[ClaimAssessment]]:
    """Gate and drain share the same bounded batching and ordering policy."""
    if not groups:
        return []
    if spec.batch_assessor is not None:
        return _assess_grouped_batches(
            groups,
            passages,
            batch_assessor=spec.batch_assessor,
            assessor_id=spec.assessor_id,
            parallel=parallel,
        )
    flat = [(index, claim) for index, group in enumerate(groups) for claim in group]
    if not flat:
        return [[] for _ in groups]
    results = _assess_flat_claims(
        flat,
        passages,
        assessor=spec.assessor,
        assessor_id=spec.assessor_id,
        parallel=parallel,
    )
    return _regroup_assessments(groups, flat, results)


def _assess_grouped_batches(
    groups: Sequence[Sequence[str]],
    passages: Sequence[EvidencePassage],
    *,
    batch_assessor: BatchAssessor,
    assessor_id: str,
    parallel: bool,
) -> list[list[ClaimAssessment]]:
    """Batch concurrency follows the same policy as individual assessments."""

    def _assess_one(group: Sequence[str]) -> list[ClaimAssessment]:
        return assess_claims_batch(
            group,
            passages,
            batch_assessor=batch_assessor,
            assessor_id=assessor_id,
        )

    if parallel and len(groups) > 1:
        from co_scientist.core.async_bridge import propagate_context

        with ThreadPoolExecutor(max_workers=min(ASSESSMENT_CONCURRENCY, len(groups))) as pool:
            return list(pool.map(propagate_context(_assess_one), groups))
    return [_assess_one(group) for group in groups]


def _regroup_assessments(
    groups: Sequence[Sequence[str]],
    flat: list[tuple[int, str]],
    results: list[ClaimAssessment],
) -> list[list[ClaimAssessment]]:
    grouped: list[list[ClaimAssessment]] = [[] for _ in groups]
    for (index, _claim), assessment in zip(flat, results, strict=True):
        grouped[index].append(assessment)
    return grouped


def _assess_flat_claims(
    flat: list[tuple[int, str]],
    passages: Sequence[EvidencePassage],
    *,
    assessor: Assessor,
    assessor_id: str,
    parallel: bool,
) -> list[ClaimAssessment]:

    def _assess_one(item: tuple[int, str]) -> ClaimAssessment:
        return assess_claim(item[1], passages, assessor=assessor, assessor_id=assessor_id)

    if parallel:
        from co_scientist.core.async_bridge import propagate_context

        # ThreadPoolExecutor does not copy contextvars; each call carries caller
        # budget
        # and telemetry scope explicitly.
        with ThreadPoolExecutor(max_workers=min(ASSESSMENT_CONCURRENCY, len(flat))) as pool:
            return list(pool.map(propagate_context(_assess_one), flat))
    return [_assess_one(item) for item in flat]


def assess_hypothesis_claims(
    hyps: Sequence[Mapping[str, Any]],
    passages: Sequence[EvidencePassage],
    spec: AssessorSpec | None = None,
    *,
    reuse: Mapping[str, Mapping[str, ClaimAssessment]] | None = None,
) -> list[tuple[str, list[tuple[ClaimAssessment, str]]]]:
    """Assessment happens before database writes: provider I/O must never
    hold SQLite's single writer.
    """
    spec = spec or AssessorSpec()
    candidates = [p for p in passages if p.text]
    per_hypothesis = _per_hypothesis_claim_records(hyps)
    plans = [
        _plan_claim_group(hyp_id, records, candidates, spec.assessor_id, reuse or {})
        for hyp_id, records in per_hypothesis
    ]
    keys = [
        [
            claim_fingerprint(ClaimRecord(claim, role), candidates, spec.assessor_id)
            for claim, role in plan.records
            if claim in plan.to_assess
        ]
        for plan in plans
    ]
    # Ideas restating one claim against the same evidence share one check.
    first: dict[str, tuple[int, int]] = {}
    groups: list[list[str]] = []
    for plan_index, (plan, plan_keys) in enumerate(zip(plans, keys, strict=True)):
        group: list[str] = []
        for claim, key in zip(plan.to_assess, plan_keys, strict=True):
            if key not in first:
                first[key] = (plan_index, len(group))
                group.append(claim)
        groups.append(group)
    grouped = assess_claim_groups(groups, candidates, spec)
    shared = sum(len(plan.to_assess) for plan in plans) - sum(len(group) for group in groups)
    if shared:
        logger.info("Claim grounding: %s claim checks shared across ideas", shared)
    return [
        (
            plan.hypothesis_id,
            plan.merge([grouped[first[key][0]][first[key][1]] for key in plan_keys]),
        )
        for plan, plan_keys in zip(plans, keys, strict=True)
    ]


@dataclasses.dataclass(frozen=True)
class _ClaimGroupPlan:
    hypothesis_id: str
    records: list[tuple[str, str]]
    reused: dict[str, ClaimAssessment]
    to_assess: list[str]

    def merge(self, assessed: Sequence[ClaimAssessment]) -> list[tuple[ClaimAssessment, str]]:
        pending = iter(assessed)
        return [(self.reused.get(claim) or next(pending), role) for claim, role in self.records]


def _plan_claim_group(
    hypothesis_id: str,
    records: list[tuple[str, str]],
    candidates: Sequence[EvidencePassage],
    assessor_id: str,
    reuse: Mapping[str, Mapping[str, ClaimAssessment]],
) -> _ClaimGroupPlan:

    available = reuse.get(hypothesis_id) or {}
    reused: dict[str, ClaimAssessment] = {}
    if available:
        for claim, role in records:
            fingerprint = claim_fingerprint(ClaimRecord(claim, role), candidates, assessor_id)
            match = available.get(fingerprint)
            rebased = _rebased(match, candidates) if match is not None else None
            if rebased is not None:
                reused[claim] = rebased
    return _ClaimGroupPlan(
        hypothesis_id=hypothesis_id,
        records=records,
        reused=reused,
        to_assess=[claim for claim, _role in records if claim not in reused],
    )


def _rebased(
    assessment: ClaimAssessment, passages: Sequence[EvidencePassage]
) -> ClaimAssessment | None:
    """A span that matches no current passage cannot be cited, so its claim
    is checked again."""

    def rebase(span: SupportSpan) -> SupportSpan | None:
        for passage in passages:
            if (
                passage.source == span.source
                and passage.url == span.url
                and passage.text[span.start : span.end] == span.quote
            ):
                return dataclasses.replace(span, evidence_id=str(passage.evidence_id))
        return None

    supporting = [rebase(span) for span in assessment.supporting_passages]
    contradicting = [rebase(span) for span in assessment.contradicting_passages]
    if None in supporting or None in contradicting:
        return None
    return dataclasses.replace(
        assessment,
        supporting_passages=tuple(span for span in supporting if span is not None),
        contradicting_passages=tuple(span for span in contradicting if span is not None),
    )


def _per_hypothesis_claim_records(
    hyps: Sequence[Mapping[str, Any]],
) -> list[tuple[str, list[tuple[str, str]]]]:
    return [(hyp_id, _claim_records(hyp)) for hyp in hyps if (hyp_id := str(hyp.get("id") or ""))]


def build_assessor(model: str) -> tuple[Assessor, str]:
    # Offline admission precedes provider dispatch even when credentials are
    # available.

    from co_scientist.platform.llm.process_mode import offline_mode

    if not offline_mode():
        from co_scientist.domains.research_state.claims.verifier import make_llm_assessor

        return make_llm_assessor(model)
    return deterministic_assessor, _ASSESSOR_DETERMINISTIC


def build_batch_assessor(
    model: str, *, call_counter: list[int] | None = None
) -> BatchAssessor | None:
    """Offline guards cover batch and individual paths so batching cannot
    expose goals to a provider.
    """
    from co_scientist.platform.llm.process_mode import offline_mode

    if not offline_mode():
        from co_scientist.domains.research_state.claims.verifier import make_llm_batch_assessor

        batch_assessor, _ = make_llm_batch_assessor(model, call_counter=call_counter)
        return batch_assessor
    return None


def evidence_passages(
    run_id: str,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> list[EvidencePassage]:
    """Offsets index persisted passage_text."""
    from co_scientist.domains.research_state.claims.chunking import chunk_evidence_passage

    passages: list[EvidencePassage] = []
    for ev in store.list_evidence(run_id, conn=conn, db_path=db_path):
        if not ev.get("available"):
            continue
        text = str(ev["passage_text"] or "").strip()
        if not text:
            continue
        passages.extend(
            chunk_evidence_passage(
                str(ev.get("id") or ""),
                head_text=text,
                body_text="",
                source=str(ev.get("source") or ""),
                url=str(ev.get("url") or ""),
            )
        )
    return passages


@dataclasses.dataclass(frozen=True)
class GroundingResult:
    """Advisory blocked IDs describe findings, not automatic publication
    exclusions.
    """

    # These IDs describe advisory findings, not automatic publication
    # exclusions.
    blocked_ids: frozenset[str]
    reason_by_id: Mapping[str, str]

    @property
    def blocked_count(self) -> int:
        return len(self.blocked_ids)


def persist_grounding(
    run_id: str,
    assessed: Sequence[tuple[str, list[tuple[ClaimAssessment, str]]]],
    *,
    allow_speculative: bool = False,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> GroundingResult:
    """Persistence performs database work only; assessment and all provider
    I/O must finish beforehand.
    """
    blocked: set[str] = set()
    unverified = 0
    reason_by_id: dict[str, str] = {}
    for hyp_id, assessments in assessed:
        gate = _ground_one_hypothesis(
            run_id,
            hyp_id,
            assessments,
            allow_speculative=allow_speculative,
            conn=conn,
            db_path=db_path,
        )
        reason_by_id[hyp_id] = gate.reason
        if gate.decision is GateDecision.BLOCK:
            blocked.add(hyp_id)
            if not _has_supported_claim(assessments):
                unverified += 1
    _log_gate_outcome(len(blocked), unverified, len(reason_by_id))
    return GroundingResult(blocked_ids=frozenset(blocked), reason_by_id=reason_by_id)


def _has_supported_claim(
    assessments: Sequence[tuple[ClaimAssessment, str]],
) -> bool:
    """A failed gate does not make supported or partially supported claims
    unverified.
    """
    return any(assessment.label.is_supporting for assessment, _role in assessments)


def _log_gate_outcome(blocked_count: int, unverified_count: int, gated_count: int) -> None:
    """Unsupported publication is advisory; only a complete absence of
    supported claims warrants an unverified label.
    """
    if not gated_count:
        return
    if unverified_count == gated_count:
        logger.warning(
            "No hypothesis cleared the claim gate: all %s published unverified",
            gated_count,
        )
        return
    logger.info(
        "Claim gate: %s of %s hypotheses failed "
        "(%s published unverified, the rest with unsupported claims flagged)",
        blocked_count,
        gated_count,
        unverified_count,
    )


def _ground_one_hypothesis(
    run_id: str,
    hyp_id: str,
    assessments: list[tuple[ClaimAssessment, str]],
    *,
    allow_speculative: bool,
    conn: sqlite3.Connection | None,
    db_path: str | None,
) -> GateResult:
    """Persisting a verdict does not itself withhold unsupported proposals;
    report release handles contradictions.
    """
    _persist_claim_edges(run_id, hyp_id, assessments, conn=conn, db_path=db_path)
    gate = publication_gate(
        [assessment for assessment, _role in assessments],
        allow_speculative=allow_speculative,
        explicitly_speculative_claims={
            assessment.claim for assessment, role in assessments if role == ClaimRole.SPECULATIVE
        },
        require_supported_claim=not allow_speculative,
    )
    if gate.decision is GateDecision.BLOCK:
        # Recorded failures use the gate's authoritative failed_claims rather
        # than a second approximation.
        store.add_safety_decision(
            NewSafetyDecision(
                run_id=run_id,
                stage="claim_gate",
                decision="block",
                reason=f"hypothesis {hyp_id}: {gate.reason}",
                matches=list(gate.failed_claims),
            ),
            db_path=db_path,
            conn=conn,
        )
        # Unsupported proposals still publish; logs must not claim they were
        # quarantined or escalate normal findings per item.
        logger.info(
            "Hypothesis %s did not clear the claim gate (%s): %s",
            hyp_id,
            _publication_outcome(gate, assessments),
            gate.reason,
        )
    return gate


def _publication_outcome(
    gate: GateResult, assessments: Sequence[tuple[ClaimAssessment, str]]
) -> str:
    if gate.failed_claims and set(gate.failed_claims) <= set(gate.contradicted_claims):
        return "withheld from the report"
    if _has_supported_claim(assessments):
        return "published with unsupported claims flagged"
    return "published unverified"


def _persist_claim_edges(
    run_id: str,
    hyp_id: str,
    assessments: list[tuple[ClaimAssessment, str]],
    *,
    conn: sqlite3.Connection | None,
    db_path: str | None,
) -> None:
    for assessment, role in assessments:
        store.add_claim_evidence(
            NewClaimEvidence(
                run_id=run_id,
                hypothesis_id=hyp_id,
                claim=assessment.claim,
                label=assessment.label.value,
                supporting=[s.to_dict() for s in assessment.supporting_passages],
                contradicting=[s.to_dict() for s in assessment.contradicting_passages],
                assessor=assessment.assessor,
                verification_method=assessment.verification_method,
                claim_role=role,
            ),
            db_path=db_path,
            conn=conn,
        )


__all__ = ["AssessorSpec", "assess_claim_groups", "assess_hypothesis_claims"]
