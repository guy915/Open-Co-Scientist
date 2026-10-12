from __future__ import annotations

import functools
import logging
import sqlite3
from collections.abc import Mapping
from typing import Any, NamedTuple

from co_scientist.core.config import settings
from co_scientist.domains.research_state.claims import EvidencePassage
from co_scientist.domains.research_state.claims.grounding import (
    AssessorSpec,
    assess_hypothesis_claims,
    build_assessor,
    build_batch_assessor,
    evidence_passages,
    persist_grounding,
)
from co_scientist.domains.research_state.drain.hypotheses import (
    _hypotheses_with_proximity_archive,
    _HypothesisSink,
    _persist_evidence_and_hypotheses,
    resolve_articles,
)
from co_scientist.domains.research_state.drain.matches import (
    _persist_engine_matches,
    _persist_engine_proximity,
    _persist_retrieval_calls,
)
from co_scientist.domains.research_state.drain.reviews import _CitationSink
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.domains.research_state.repository import records as store_records
from co_scientist.domains.research_state.repository.records import NewSafetyDecision
from co_scientist.domains.safety.hypothesis import (
    persist_escalated_verdicts,
    screen_hypotheses,
)
from co_scientist.domains.safety.hypothesis.safety import (
    POLICY_VERSION,
    HypothesisSafetyOutcome,
    escalate_held_hypotheses,
)
from co_scientist.platform import db
from co_scientist.platform.db import supervisor_plan as plans
from co_scientist.platform.db.supervisor_plan import NewSupervisorPlan
from co_scientist.platform.retrieval.citations import empty_citation_summary

logger = logging.getLogger(__name__)


class FinalStateInputs(NamedTuple):
    """Raw final state remains available for persistence facts not
    represented in precomputed inputs.
    """

    hyps_parents_first: list[dict[str, Any]]
    articles: list[dict[str, Any]]
    matchups: list[dict[str, Any]]
    proximity_graph: dict[str, Any]
    persisted_engine_ids: set[str]
    final_state: dict[str, Any]


def degraded_sections(final_state: dict[str, Any]) -> list[str]:
    """Blank output caused by schema fallback must be disclosed rather than
    silently appearing complete.
    """
    return [str(name) for name in final_state.get("degraded_nodes") or []]


def retrieval_degradation(
    final_state: dict[str, Any],
) -> dict[str, Any] | None:
    """Unattempted source retrieval is a different fact from synthesis
    degradation and cannot be inferred from report prose.
    """
    degradation = final_state.get("retrieval_degradation")
    return degradation if isinstance(degradation, dict) else None


def skills_used(final_state: dict[str, Any]) -> dict[str, int]:
    """Third-party source terms require attribution; transient workspaces
    disappear, so the report carries actual source usage.
    """
    metrics = final_state.get("metrics")
    used = getattr(metrics, "skills_used", None)
    if used is None and isinstance(metrics, dict):
        used = metrics.get("skills_used")
    if not isinstance(used, dict):
        return {}
    return {str(name): int(count) for name, count in used.items()}


def stratification_attributes(
    final_state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Supervisor guidance is display-only and never gates, ranks or
    disqualifies hypotheses; older checkpoints may omit it.
    """
    guidance = final_state.get("supervisor_guidance")
    config = guidance.get("config_synthesis") if isinstance(guidance, dict) else None
    attributes = config.get("attributes") if isinstance(config, dict) else None
    if not isinstance(attributes, list):
        return []
    return [attr for attr in attributes if isinstance(attr, dict)]


def critical_criteria(final_state: dict[str, Any]) -> list[Any]:
    """Display-only guidance never gates or ranks hypotheses; legacy
    checkpoints may contain bare criterion names.
    """
    guidance = final_state.get("supervisor_guidance")
    plan = guidance.get("workflow_plan") if isinstance(guidance, dict) else None
    review_phase = plan.get("review_phase") if isinstance(plan, dict) else None
    criteria = review_phase.get("critical_criteria") if isinstance(review_phase, dict) else None
    if not isinstance(criteria, list):
        return []
    return [item for item in criteria if isinstance(item, (str, dict))]


def grounding_counts(
    grounding_result: Any, grounding_candidates: list[dict[str, Any]]
) -> dict[str, int]:
    """Assessed counts include blocked hypotheses; calling them grounded
    would double-count failed outcomes.
    """
    assessed = len(grounding_result.reason_by_id)
    return {
        "assessed": assessed,
        "grounded": assessed - grounding_result.blocked_count,
        "blocked": grounding_result.blocked_count,
        "eligible": (len(grounding_candidates) - grounding_result.blocked_count),
    }


def safety_counts(screening_result: Any) -> dict[str, int]:
    return {
        "screened": screening_result.screened_count,
        "blocked": screening_result.blocked_count,
        "eligible": (screening_result.screened_count - screening_result.blocked_count),
    }


async def _assess_claims(
    grounding_candidates: list[dict[str, Any]],
    passages: list[EvidencePassage],
    gate_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[Any, dict[str, dict[str, Any]]]:
    """Reuse requires matching per-claim evidence inputs; provider work
    stays off-loop so the finalize lease keeps renewing.
    """
    from co_scientist.core.async_bridge import run_off_loop
    from co_scientist.platform.llm import scoped_telemetry

    model = settings.claim_verifier_model or settings.model_name
    assert model is not None
    assessor, assessor_id = build_assessor(model)
    batch_assessor = build_batch_assessor(model)
    spec = AssessorSpec(assessor, assessor_id, batch_assessor)
    call = functools.partial(
        assess_hypothesis_claims,
        grounding_candidates,
        passages,
        spec,
        reuse=_reusable_by_hypothesis(gate_records or {}),
    )
    with scoped_telemetry("claim_grounding") as telemetry:
        assessed = await run_off_loop(call)
    return assessed, telemetry.snapshot()


def _depth_reviewed_candidates(
    candidates: list[dict[str, Any]],
    inputs: FinalStateInputs,
    store_id_by_engine_id: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Claim checks are finalist depth; a screened idea stays unassessed
    rather than reading as unsupported."""
    reviewed = {
        store_id_by_engine_id.get(str(hypothesis.get("id") or ""))
        for hypothesis in inputs.hyps_parents_first
        if hypothesis.get("deep_verification_verdict") is not None
    }
    return [candidate for candidate in candidates if candidate.get("id") in reviewed]


def _gate_records_by_store_id(
    inputs: FinalStateInputs,
    store_id_by_engine_id: Mapping[str, str],
) -> dict[str, Mapping[str, Any]]:
    """Gate enrichments use engine IDs; persisted assessment rows join
    through the drain's engine-to-store map.
    """
    records: dict[str, Mapping[str, Any]] = {}
    for hypothesis in inputs.hyps_parents_first:
        engine_id = str(hypothesis.get("id") or "")
        store_id = store_id_by_engine_id.get(engine_id)
        gate = (hypothesis.get("enrichments") or {}).get("claim_gate")
        if store_id and isinstance(gate, Mapping):
            records[store_id] = gate
    return records


def _reusable_by_hypothesis(
    gate_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    from co_scientist.domains.research_state.claims.grounding import reusable_assessments

    indexed = {store_id: reusable_assessments(record) for store_id, record in gate_records.items()}
    return {key: value for key, value in indexed.items() if value}


def fold_grounding_telemetry(final_state: dict[str, Any], usage: dict[str, dict[str, Any]]) -> None:
    if not usage:
        return
    from co_scientist.core.metrics import (
        ExecutionMetrics,
        create_metrics_update,
        merge_metrics,
    )
    from co_scientist.domains.research_state.models import MetricDeltas

    calls = sum(entry.get("calls", 0) for entry in usage.values())
    existing = ExecutionMetrics.from_dict(final_state.get("metrics") or {})
    delta = create_metrics_update(deltas=MetricDeltas(llm_calls=calls), model_usage=usage)
    merged = merge_metrics(existing, delta)
    final_state["metrics"] = merged.to_dict()


# Retain enough held hypothesis text for human identification, bounded like
# engine audit entries.
_HELD_TEXT_PREFIX_CHARS = 120


def _final_state_list(final_state: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return final_state.get(key) or []


def _engine_audit_by_hypothesis_id(
    final_state: dict[str, Any],
) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("hypothesis_id")): item
        for item in _final_state_list(final_state, "safety_decisions")
        if isinstance(item, Mapping) and item.get("hypothesis_id")
    }


def _held_decision_reason(
    hyp_id: str,
    outcome: str,
    text: str,
    audit: Mapping[str, Any],
) -> str:
    """Held hypotheses have no persisted pool row, so the decision retains
    enough text for human identification.
    """
    engine_reason = str(audit.get("reason") or "") or (
        "the safety screen held this hypothesis for manual review"
    )
    reason = f"hypothesis {hyp_id}: {outcome} ({engine_reason})"
    prefix = text[:_HELD_TEXT_PREFIX_CHARS]
    if prefix:
        reason = f"{reason}; idea: {prefix}"
    return reason


def _held_decision(
    run_id: str,
    entry: Mapping[str, Any],
    audit_by_id: Mapping[str, Mapping[str, Any]],
) -> NewSafetyDecision | None:
    hyp_id = str(entry.get("id") or "")
    text = str(entry.get("text") or "")
    if not hyp_id and not text:
        return None
    audit = audit_by_id.get(hyp_id, {})
    outcome = str(entry.get("safety_status") or HypothesisSafetyOutcome.UNCERTAIN.value)
    return NewSafetyDecision(
        run_id=run_id,
        stage="hypothesis",
        decision="hold",
        reason=_held_decision_reason(hyp_id, outcome, text, audit),
        matches=[str(m) for m in (audit.get("matches") or [])],
        category=outcome,
        policy_version=(str(audit.get("policy_version") or "") or POLICY_VERSION),
        requires_review=True,
        assessor="engine:safety_screen",
    )


def _persist_held_for_review(
    run_id: str,
    final_state: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    held = _final_state_list(final_state, "held_for_review")
    if not held:
        return
    audit_by_id = _engine_audit_by_hypothesis_id(final_state)
    recorded = 0
    for entry in held:
        if not isinstance(entry, Mapping):
            continue
        decision = _held_decision(run_id, entry, audit_by_id)
        if decision is None:
            continue
        store_records.add_safety_decision(decision, conn=conn)
        recorded += 1
    logger.info(
        "Recorded %d held-for-review decision(s) for run %s.",
        recorded,
        run_id,
    )


class DrainResult(NamedTuple):
    report_inputs: dict[str, Any]
    safety_counts: dict[str, int]
    grounding_counts: dict[str, int]


def _screen_and_collect_grounding_inputs(
    run_id: str, conn: sqlite3.Connection
) -> tuple[Any, list[EvidencePassage], list[dict[str, Any]]]:
    """The app boundary repeats deterministic safety admission before lock-
    free model escalation.
    """
    from co_scientist.domains.report.gates import EXCLUDED_HYPOTHESIS_STATUSES

    persisted = hypotheses.list_hypotheses(run_id, conn=conn)
    screening_result = screen_hypotheses(run_id, persisted, conn=conn)
    passages = evidence_passages(run_id, conn=conn)
    grounding_candidates = [
        hypothesis
        for hypothesis in persisted
        if hypothesis.get("status") not in EXCLUDED_HYPOTHESIS_STATUSES
    ]
    return screening_result, passages, grounding_candidates


def _prepare_final_state_inputs(
    final_state: dict[str, Any],
) -> FinalStateInputs:
    """Parents insert before children; references survive only when the
    corresponding parent is also persisted.
    """
    hyps = _hypotheses_with_proximity_archive(
        final_state.get("hypotheses") or [],
        final_state.get("removed_duplicates") or [],
    )
    persisted_engine_ids = {hid for h in hyps if (hid := h.get("id"))}
    hyps_parents_first = sorted(hyps, key=lambda h: int(h.get("generation", 0)))
    return FinalStateInputs(
        hyps_parents_first=hyps_parents_first,
        articles=final_state.get("articles") or [],
        matchups=final_state.get("tournament_matchups") or [],
        proximity_graph=final_state.get("proximity_graph") or {},
        persisted_engine_ids=persisted_engine_ids,
        final_state=final_state,
    )


async def _persist_evidence_hypotheses_and_screen(
    run_id: str,
    inputs: FinalStateInputs,
    citation_summary: dict[str, int],
    store_id_by_engine_id: dict[str, str],
    db_path: str | None,
) -> tuple[Any, list[EvidencePassage], list[dict[str, Any]]]:
    """Citation resolution happens before the transaction and off-loop;
    network latency must not hold the writer or expire the lease.
    """
    from co_scientist.core.async_bridge import run_off_loop

    resolved = await run_off_loop(functools.partial(resolve_articles, inputs.articles))
    sink = _HypothesisSink(
        citations=_CitationSink(
            ev_id_by_title={},
            abstract_by_title={},
            citation_summary=citation_summary,
        ),
        store_id_by_engine_id=store_id_by_engine_id,
        persisted_engine_ids=inputs.persisted_engine_ids,
    )
    with db.transaction(db_path) as conn:
        # Persist search calls before evidence rows that reference their
        # provenance.
        _persist_retrieval_calls(run_id, inputs.final_state, conn)
        _persist_evidence_and_hypotheses(
            run_id,
            inputs.articles,
            resolved,
            inputs.hyps_parents_first,
            sink,
            conn,
        )
        result = _screen_and_collect_grounding_inputs(run_id, conn)
        _persist_held_for_review(run_id, inputs.final_state, conn)
        final_state = inputs.final_state
        plans.save_supervisor_plan(
            NewSupervisorPlan(
                run_id=run_id,
                guidance=final_state.get("supervisor_guidance") or {},
                termination_reason=final_state.get("termination_reason"),
                decision_provenance=final_state.get("supervisor_decision_provenance"),
                orchestrator_state=final_state.get("orchestrator_state") or {},
            ),
            conn=conn,
        )
        plans.replace_supervisor_allocations(
            run_id, final_state.get("task_history") or [], conn=conn
        )
        return result


def _persist_grounding_matches_proximity_txn(
    run_id: str,
    provider_outputs: tuple[Any, list[Any]],
    inputs: FinalStateInputs,
    store_id_by_engine_id: dict[str, str],
    db_path: str | None,
) -> Any:
    assessed, escalated = provider_outputs
    with db.transaction(db_path) as conn:
        grounding_result = persist_grounding(run_id, assessed, conn=conn)
        _persist_engine_matches(run_id, inputs.matchups, store_id_by_engine_id, conn)
        _persist_engine_proximity(run_id, inputs.proximity_graph, store_id_by_engine_id, conn)
        persist_escalated_verdicts(run_id, escalated, conn=conn)
        return grounding_result


async def persist_final_state(
    *,
    run_id: str,
    final_state: dict[str, Any],
    db_path: str | None = None,
) -> DrainResult:
    """Provider assessment and safety escalation run between transactions
    and off-loop, preserving both writer availability and lease renewal.
    """
    inputs = _prepare_final_state_inputs(final_state)
    citation_summary = empty_citation_summary()
    store_id_by_engine_id: dict[str, str] = {}
    (
        screening_result,
        passages,
        grounding_candidates,
    ) = await _persist_evidence_hypotheses_and_screen(
        run_id, inputs, citation_summary, store_id_by_engine_id, db_path
    )
    assessed, grounding_usage = await _assess_claims(
        _depth_reviewed_candidates(grounding_candidates, inputs, store_id_by_engine_id),
        passages,
        _gate_records_by_store_id(inputs, store_id_by_engine_id),
    )
    fold_grounding_telemetry(final_state, grounding_usage)
    # Escalate between transactions and off-loop so the finalize lease keeps
    # renewing.
    from co_scientist.core.async_bridge import run_off_loop

    escalated = await run_off_loop(
        functools.partial(
            escalate_held_hypotheses,
            run_id,
            screening_result.escalatable,
            db_path=db_path,
        )
    )
    grounding_result = _persist_grounding_matches_proximity_txn(
        run_id, (assessed, escalated), inputs, store_id_by_engine_id, db_path
    )
    return DrainResult(
        report_inputs={
            "citation_summary": citation_summary,
            "meta_review": final_state.get("meta_review") or {},
            "research_overview": final_state.get("research_overview") or {},
            "degraded_sections": degraded_sections(final_state),
            "retrieval_degradation": retrieval_degradation(final_state),
            "skills_used": skills_used(final_state),
            "attributes": stratification_attributes(final_state),
            "critical_criteria": critical_criteria(final_state),
        },
        safety_counts=safety_counts(screening_result),
        grounding_counts=grounding_counts(grounding_result, grounding_candidates),
    )
