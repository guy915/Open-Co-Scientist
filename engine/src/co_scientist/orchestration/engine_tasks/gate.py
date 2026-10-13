from __future__ import annotations

import dataclasses
import functools
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from co_scientist.core.config import settings
from co_scientist.domains.research_state.claims.gate import ClaimRole

if TYPE_CHECKING:
    from co_scientist.domains.research_state.state import WorkflowState

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _GatePlan:
    hypothesis: Any
    claims: tuple[str, ...]
    roles: Mapping[str, str]
    fingerprint: str
    claim_fingerprints: Mapping[str, str]
    prior_disposition: str
    # Verdicts whose inputs are unchanged: this idea's own earlier check or
    # another idea's check of the same claim against the same evidence.
    reused: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    # The idea whose check each reused verdict is, when it is not this one.
    reused_from: Mapping[str, str] = dataclasses.field(default_factory=dict)

    @property
    def to_assess(self) -> list[str]:
        return [claim for claim in self.claims if claim not in self.reused]


# Use a dedicated off-loop bridge: the process-wide thread executor already
# carries long-lived worker cohorts.
@dataclasses.dataclass(frozen=True)
class _GateAssessments:
    assessments: list[Any]
    reused_from: dict[str, str]


async def _assess_gate_claims(
    plans: Sequence[_GatePlan],
    passages: Sequence[Any],
    spec: Any,
) -> list[_GateAssessments]:
    """Gate and drain share one bounded assessment policy, preserving claim
    ordering and provenance. A claim several ideas state against the same
    evidence is checked once per wave."""
    from co_scientist.core.async_bridge import run_off_loop
    from co_scientist.domains.research_state.claims.grounding import assess_claim_groups
    from co_scientist.platform.llm.process_mode import offline_mode

    owners: dict[str, str] = {}
    groups: list[list[str]] = []
    for plan in plans:
        group = []
        for claim in plan.to_assess:
            fingerprint = plan.claim_fingerprints[claim]
            if fingerprint not in owners:
                owners[fingerprint] = plan.hypothesis.id
                group.append(claim)
        groups.append(group)
    call = functools.partial(assess_claim_groups, groups, passages, spec)
    assessed = call(parallel=False) if offline_mode() else await run_off_loop(call)
    by_fingerprint = {
        plan.claim_fingerprints[claim]: assessment
        for plan, group, results in zip(plans, groups, assessed, strict=True)
        for claim, assessment in zip(group, results, strict=True)
    }
    return [_merged_plan_assessments(plan, by_fingerprint, owners) for plan in plans]


def _merged_plan_assessments(
    plan: _GatePlan, by_fingerprint: Mapping[str, Any], owners: Mapping[str, str]
) -> _GateAssessments:
    reused_from = dict(plan.reused_from)
    assessments = []
    for claim in plan.claims:
        if claim in plan.reused:
            assessments.append(plan.reused[claim])
            continue
        fingerprint = plan.claim_fingerprints[claim]
        assessments.append(by_fingerprint[fingerprint])
        if owners[fingerprint] != plan.hypothesis.id:
            reused_from[claim] = owners[fingerprint]
    return _GateAssessments(assessments, reused_from)


def _build_evidence_passages(state: dict[str, Any]) -> list[Any]:
    """Full text is chunked for passage-specific grounding; title and
    abstract retain their single evidence span.
    """
    from co_scientist.domains.research_state.claims import EvidencePassage
    from co_scientist.domains.research_state.claims.chunking import chunk_evidence_passage

    passages: list[EvidencePassage] = []
    for article in state.get("articles") or []:
        head_text = " ".join(part for part in (article.title, article.abstract) if part)
        passages.extend(
            chunk_evidence_passage(
                str(article.source_id or article.title),
                head_text=head_text,
                body_text=str(article.content or ""),
                source=article.source,
                url=str(article.url or ""),
            )
        )
    # Scientist-uploaded private sources are admissible grounding evidence
    # alongside retrieved literature.
    for source in state.get("context_enrichment_sources") or []:
        data = source.get("data") or {}
        text = str(source.get("display") or data.get("excerpt") or "").strip()
        if not text:
            continue
        passages.extend(
            chunk_evidence_passage(
                str(data.get("document_id") or data.get("title") or "private"),
                head_text="",
                body_text=text,
                source=str(source.get("source_type") or "private_document"),
                url="",
            )
        )
    return passages


def _harvest_hypothesis_claims(
    hypothesis: Any,
) -> tuple[list[str], dict[str, str]]:
    from co_scientist.domains.research_state.claims import extract_atomic_claims

    claim_roles: dict[str, str] = {}
    ordered_claims: list[str] = []
    for source_text, role in (
        (hypothesis.text, ClaimRole.SPECULATIVE.value),
        (hypothesis.literature_grounding, ClaimRole.CATEGORICAL.value),
        (hypothesis.explanation, ClaimRole.SPECULATIVE.value),
        (hypothesis.experiment, ClaimRole.SPECULATIVE.value),
    ):
        for claim in extract_atomic_claims(source_text or ""):
            if claim not in claim_roles:
                ordered_claims.append(claim)
                claim_roles[claim] = role
            elif role != ClaimRole.SPECULATIVE:
                # The strict role wins when a sentence occurs under both claim
                # roles.
                claim_roles[claim] = role
    return ordered_claims, claim_roles


def _per_claim_fingerprints(
    assessor_id: str,
    ordered_claims: Sequence[str],
    claim_roles: Mapping[str, str],
    passages: Sequence[Any],
) -> dict[str, str]:
    """Only evidence shown to a claim can invalidate its cached verdict;
    reuse the same digests at both cache levels.
    """
    from co_scientist.domains.research_state.claims.grounding import ClaimRecord, claim_fingerprint

    return {
        claim: claim_fingerprint(ClaimRecord(claim, claim_roles[claim]), passages, assessor_id)
        for claim in ordered_claims
    }


def _permanently_unrankable(hypothesis: Any) -> bool:
    """Evidence blocks remain reassessable; irreversible initial-review
    exclusions, including covered prior dispositions, do not.
    """
    if hypothesis.is_rankable():
        return False
    if hypothesis.review_disposition != "evidence_blocked":
        return True
    from co_scientist.domains.research_state.models import BLOCKING_REVIEW_DISPOSITIONS

    gate_history = hypothesis.enrichments.get("claim_gate") or {}
    underlying = gate_history.get("prior_review_disposition")
    return underlying in BLOCKING_REVIEW_DISPOSITIONS - {"evidence_blocked"}


def _plan_hypothesis_gate(
    hypothesis: Any,
    passages: Sequence[Any],
    assessor_id: str,
    run_verdicts: Mapping[str, tuple[Any, str]] | None = None,
) -> _GatePlan | None:
    from co_scientist.domains.research_state.claims import GateDecision

    gate_history = hypothesis.enrichments.get("claim_gate") or {}
    prior_disposition = str(
        gate_history.get("prior_review_disposition") or hypothesis.review_disposition or "viable"
    )
    from co_scientist.domains.research_state.claims.grounding import combined_fingerprint

    ordered_claims, claim_roles = _harvest_hypothesis_claims(hypothesis)
    claim_fingerprints = _per_claim_fingerprints(assessor_id, ordered_claims, claim_roles, passages)
    input_fingerprint = combined_fingerprint(
        [claim_fingerprints[claim] for claim in ordered_claims]
    )
    if gate_history.get("input_fingerprint") == input_fingerprint:
        if gate_history.get("decision") == GateDecision.BLOCK.value:
            hypothesis.review_disposition = "evidence_blocked"
        elif hypothesis.review_disposition == "evidence_blocked":
            hypothesis.review_disposition = prior_disposition
        return None
    return _GatePlan(
        hypothesis=hypothesis,
        claims=tuple(ordered_claims),
        roles=claim_roles,
        fingerprint=input_fingerprint,
        claim_fingerprints=claim_fingerprints,
        prior_disposition=prior_disposition,
        **_reusable_for(
            hypothesis.id,
            claim_fingerprints,
            # An idea's own earlier check is its provenance before any other's.
            {**(run_verdicts or {}), **_run_verdicts([hypothesis], assessor_id)},
        ),
    )


def _reusable_for(
    hypothesis_id: str,
    claim_fingerprints: Mapping[str, str],
    run_verdicts: Mapping[str, tuple[Any, str]],
) -> dict[str, Any]:
    """New evidence for one claim must not repay its siblings, and a child
    restating its parent's claim against the same evidence must not repay
    the parent's check."""
    reused: dict[str, Any] = {}
    reused_from: dict[str, str] = {}
    for claim, fingerprint in claim_fingerprints.items():
        found = run_verdicts.get(fingerprint)
        if found is None:
            continue
        reused[claim], source = found
        if source != hypothesis_id:
            reused_from[claim] = source
    return {"reused": reused, "reused_from": reused_from}


def _run_verdicts(hypotheses: Sequence[Any], assessor_id: str) -> dict[str, tuple[Any, str]]:
    """Every stored gate verdict in the run, by its own input fingerprint,
    with the idea whose check originally produced it."""
    from co_scientist.domains.research_state.claims.grounding import reusable_assessments

    verdicts: dict[str, tuple[Any, str]] = {}
    for hypothesis in hypotheses:
        record = hypothesis.enrichments.get("claim_gate") or {}
        if record.get("assessor") != assessor_id:
            continue
        sources = {
            str(claim.get("fingerprint") or ""): str(claim.get("reused_from") or hypothesis.id)
            for claim in record.get("claims") or ()
            if isinstance(claim, Mapping)
        }
        for fingerprint, assessment in reusable_assessments(record).items():
            verdicts.setdefault(fingerprint, (assessment, sources.get(fingerprint, hypothesis.id)))
    return verdicts


def _record_gate_enrichment(
    hypothesis: Any,
    plan: _GatePlan,
    checked: _GateAssessments,
    gate: Any,
    assessor_id: str,
) -> None:
    plan_roles = plan.roles
    hypothesis.enrichments["claim_gate"] = {
        "decision": gate.decision.value,
        "reason": gate.reason,
        "assessor": assessor_id,
        "input_fingerprint": plan.fingerprint,
        "prior_review_disposition": plan.prior_disposition,
        "claims": [
            {
                "claim": assessment.claim,
                "role": plan_roles[assessment.claim],
                # Drain reuses individual claims against a later evidence pool
                # only when their own input fingerprints match.
                "fingerprint": plan.claim_fingerprints[assessment.claim],
                "label": assessment.label.value,
                "verification_method": assessment.verification_method,
                "supporting_passages": [span.to_dict() for span in assessment.supporting_passages],
                "contradicting_passages": [
                    span.to_dict() for span in assessment.contradicting_passages
                ],
                **(
                    {"reused_from": checked.reused_from[assessment.claim]}
                    if assessment.claim in checked.reused_from
                    else {}
                ),
            }
            for assessment in checked.assessments
        ],
    }


def _apply_gate_verdict(
    plan: _GatePlan,
    checked: _GateAssessments,
    assessor_id: str,
) -> None:
    """Missing support does not withhold speculative proposals;
    contradictions still block ranking and publication.
    """
    from co_scientist.domains.research_state.claims import GateDecision, publication_gate

    hypothesis = plan.hypothesis
    gate = publication_gate(
        checked.assessments,
        allow_speculative=True,
        explicitly_speculative_claims={
            claim for claim, role in plan.roles.items() if role == ClaimRole.SPECULATIVE
        },
        require_supported_claim=False,
    )
    _record_gate_enrichment(hypothesis, plan, checked, gate, assessor_id)
    if gate.decision is GateDecision.BLOCK:
        hypothesis.review_disposition = "evidence_blocked"
        feedback = f"Evidence gate: {gate.reason}"
        if feedback not in (hypothesis.reflection_notes or ""):
            hypothesis.reflection_notes = "\n".join(
                part for part in (hypothesis.reflection_notes, feedback) if part
            )
    elif hypothesis.review_disposition == "evidence_blocked":
        hypothesis.review_disposition = plan.prior_disposition


@dataclasses.dataclass(frozen=True)
class _GateWave:
    plans: list[_GatePlan]
    considered: int
    skipped_unrankable: int
    skipped_unchanged: int


def _build_gate_wave(
    hypotheses: Sequence[Any],
    passages: Sequence[Any],
    assessor_id: str,
    run_verdicts: Mapping[str, tuple[Any, str]] | None = None,
) -> _GateWave:
    """Irreversibly excluded ideas are skipped before retrieval or provider
    work that cannot change their fate.
    """
    plans: list[_GatePlan] = []
    skipped_unrankable = 0
    skipped_unchanged = 0
    for hypothesis in hypotheses:
        if _permanently_unrankable(hypothesis):
            skipped_unrankable += 1
            continue
        plan = _plan_hypothesis_gate(hypothesis, passages, assessor_id, run_verdicts)
        if plan is None:
            skipped_unchanged += 1
            continue
        plans.append(plan)
    return _GateWave(plans, len(hypotheses), skipped_unrankable, skipped_unchanged)


def _log_gate_wave(wave: _GateWave, entailment_calls: int) -> None:
    """Claims assessed and physical provider calls differ under batching;
    telemetry must not substitute one for the other.
    """
    claims_assessed = sum(len(plan.to_assess) for plan in wave.plans)
    logger.info(
        "claim gate pass: considered=%d skipped_unrankable=%d "
        "skipped_unchanged=%d assessed=%d claims_assessed=%d "
        "entailment_calls=%d",
        wave.considered,
        wave.skipped_unrankable,
        wave.skipped_unchanged,
        len(wave.plans),
        claims_assessed,
        entailment_calls,
    )


def _gated_hypotheses(state: dict[str, Any]) -> list[Any]:
    """Claim checks are depth: finalists, plus ideas a past check blocked,
    which must stay reassessable."""
    from co_scientist.science.scheduling.funnel import finalist_ids

    leaders = finalist_ids(cast("WorkflowState", state))
    return [
        hypothesis
        for hypothesis in state.get("hypotheses") or []
        if hypothesis.id in leaders or hypothesis.review_disposition == "evidence_blocked"
    ]


async def _apply_pre_ranking_evidence_gate(state: dict[str, Any]) -> None:
    from co_scientist.domains.research_state.claims.grounding import (
        AssessorSpec,
        build_assessor,
        build_batch_assessor,
    )
    from co_scientist.platform.llm import scoped_telemetry

    passages = _build_evidence_passages(state)
    model = settings.claim_verifier_model or settings.model_name
    assert model is not None
    assessor, assessor_id = build_assessor(model)
    entailment_calls = [0]
    batch_assessor = build_batch_assessor(model, call_counter=entailment_calls)
    spec = AssessorSpec(assessor, assessor_id, batch_assessor)
    wave = _build_gate_wave(
        _gated_hypotheses(state),
        passages,
        assessor_id,
        _run_verdicts(state.get("hypotheses") or [], assessor_id),
    )

    with scoped_telemetry("claim_gate") as telemetry:
        assessed = await _assess_gate_claims(wave.plans, passages, spec)
    for plan, assessments in zip(wave.plans, assessed, strict=True):
        _apply_gate_verdict(plan, assessments, assessor_id)
    _fold_gate_telemetry(state, telemetry.snapshot())
    _log_gate_wave(wave, entailment_calls[0])


def _fold_gate_telemetry(state: dict[str, Any], usage: Mapping[str, Mapping[str, Any]]) -> None:
    if not usage:
        return
    from co_scientist.core.metrics import merge_metrics
    from co_scientist.domains.research_state.models import MetricDeltas, create_metrics_update

    calls = sum(entry.get("calls", 0) for entry in usage.values())
    delta = create_metrics_update(
        deltas=MetricDeltas(llm_calls=calls),
        model_usage=cast("dict[str, dict[str, Any]]", dict(usage)),
    )
    existing = state.get("metrics")
    state["metrics"] = merge_metrics(existing, delta) if existing else delta
