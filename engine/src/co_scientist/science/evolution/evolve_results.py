import dataclasses
import logging
from typing import Any

from co_scientist.core.constants import (
    DUPLICATE_SIMILARITY_THRESHOLD,
    INITIAL_ELO_RATING,
)
from co_scientist.domains.research_state.models import (
    Hypothesis,
    HypothesisOrigin,
    MetricDeltas,
    create_metrics_update,
    phase_message,
)
from co_scientist.domains.research_state.state import AppendHypotheses
from co_scientist.science.citations import (
    format_experiment_plan,
    resolve_citation_keys,
)
from co_scientist.science.evolution.evolve_prompt import (
    _EvolutionOperation,
    find_nearest_peer,
)
from co_scientist.science.evolution.operations import EvolutionContext

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _RefinedFields:
    """Missing proposal sections belong to the child as absent, never
    inherited from the parent it may have changed."""

    refined_text: str
    title: str | None
    explanation: str | None
    experiment: str | None
    refinement_summary: str
    introduction: str | None = None
    recent_findings: str | None = None
    literature_grounding: str | None = None
    safety_and_toxicity: str | None = None


def _section_or_none(response: dict[str, Any], key: str) -> str | None:
    """json_object reshaping inserts empty strings for missing fields; blank
    child sections must not inherit potentially stale parent sections."""
    value = response.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _extract_evolution_fields(hypothesis: Hypothesis, response: dict[str, Any]) -> _RefinedFields:
    refined_text = response.get("hypothesis") or response.get(
        "refined_hypothesis_text", hypothesis.text
    )
    # Missing child title must not inherit stale parent wording; the app derives
    # its fallback from refined text. Experiment plans retain their renderer.
    return _RefinedFields(
        refined_text=refined_text,
        title=response.get("title"),
        explanation=response.get("explanation", hypothesis.explanation),
        experiment=format_experiment_plan(
            response.get("experiment"), fallback=hypothesis.experiment
        ),
        refinement_summary=response.get("refinement_summary", "no refinement summary provided"),
        introduction=_section_or_none(response, "introduction"),
        recent_findings=_section_or_none(response, "recent_findings"),
        literature_grounding=_section_or_none(response, "literature_grounding"),
        safety_and_toxicity=_section_or_none(response, "safety_and_toxicity"),
    )


def _resolve_parents(
    hypothesis: Hypothesis,
    response: dict[str, Any],
    partners: tuple[Hypothesis, ...],
) -> list[Hypothesis]:
    """One-based partner indices bound output size; malformed references
    fallback to the primary parent rather than rejecting valid refinement."""
    if not partners or response.get("_evolution_operator") != "combination":
        return [hypothesis]
    merged = _merged_partners(hypothesis, response, partners)
    return [hypothesis, *merged]


def _merged_partners(
    hypothesis: Hypothesis,
    response: dict[str, Any],
    partners: tuple[Hypothesis, ...],
) -> list[Hypothesis]:
    resolved: list[Hypothesis] = []
    seen: set[str] = set()
    for raw in response.get("combined_partners") or []:
        partner = _partner_at(partners, raw)
        if partner is None or partner.id == hypothesis.id:
            continue
        if partner.id not in seen:
            seen.add(partner.id)
            resolved.append(partner)
    return resolved


def _partner_at(partners: tuple[Hypothesis, ...], raw: Any) -> Hypothesis | None:
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    if not 1 <= raw <= len(partners):
        return None
    return partners[raw - 1]


def _build_evolution_child(
    parents: list[Hypothesis],
    fields: _RefinedFields,
    creation_iteration: int | None,
    citation_sources: dict[str, dict[str, Any]] | None = None,
) -> Hypothesis:
    """Children are immutable new entrants, unreviewed with fresh ratings.
    Resolve their own citations; parent prose and evidence may be stale."""
    primary = parents[0]
    return Hypothesis(
        text=fields.refined_text,
        title=fields.title,
        parent_id=primary.id,
        parent_ids=[parent.id for parent in parents],
        generation=primary.generation + 1,
        origin=HypothesisOrigin.EVOLUTION,
        creation_iteration=creation_iteration,
        category=primary.category,
        # Absent child sections must not publish the parent's prose under a new
        # idea.
        introduction=fields.introduction,
        recent_findings=fields.recent_findings,
        safety_and_toxicity=fields.safety_and_toxicity,
        explanation=fields.explanation,
        experiment=fields.experiment,
        literature_grounding=fields.literature_grounding,
        citation_map=resolve_citation_keys(fields.literature_grounding, citation_sources or {}),
        elo_rating=INITIAL_ELO_RATING,
        win_count=0,
        loss_count=0,
        reviews=[],
        evolution_history=[*primary.evolution_history, primary.text],
    )


def _build_evolution_detail(
    parents: list[Hypothesis],
    child: Hypothesis,
    fields: _RefinedFields,
) -> dict[str, Any]:
    return {
        "parent_id": parents[0].id,
        "parent_ids": [parent.id for parent in parents],
        "child_id": child.id,
        "original": parents[0].text,
        "evolved": fields.refined_text,
        "rationale": fields.refinement_summary,
    }


def _apply_refined_hypothesis(
    parents: list[Hypothesis],
    fields: _RefinedFields,
    max_similarity: float,
    context: EvolutionContext,
) -> tuple[Hypothesis, dict[str, Any]]:
    child = _build_evolution_child(
        parents,
        fields,
        context.creation_iteration,
        _citation_sources(context),
    )

    logger.debug(
        "evolved hypothesis into child %s (max similarity: %.2f)",
        child.id,
        max_similarity,
    )

    evolution_detail = _build_evolution_detail(parents, child, fields)
    return child, evolution_detail


def _citation_sources(
    context: EvolutionContext,
) -> dict[str, dict[str, Any]]:
    if context.reference_index is None:
        return {}
    return context.reference_index.sources


def _rejected_as_unchanged(hypothesis: Hypothesis, refined_text: str) -> bool:
    """Echoed parent text is a no-op, not a reason to mint a duplicate child."""
    if refined_text != hypothesis.text:
        return False
    logger.info("Evolution returned the hypothesis unchanged; no child")
    return True


def _near_duplicate_similarity(
    hypothesis: Hypothesis,
    refined_text: str,
    peers: list[Hypothesis],
    proximity_graph: dict[str, Any] | None,
    excluded_ids: frozenset[str],
) -> float | None:
    """False duplicate rejection permanently loses distinct work; keep the
    guard conservative and exempt legitimate combination partners."""
    guarded_peers = [peer for peer in peers if peer.id not in excluded_ids]
    max_similarity, nearest = find_nearest_peer(
        refined_text,
        hypothesis.id,
        guarded_peers,
        proximity_graph,
    )
    if max_similarity <= DUPLICATE_SIMILARITY_THRESHOLD:
        return max_similarity
    logger.info(
        "Evolution result duplicates an existing hypothesis (similarity %.2f); no child",
        max_similarity,
    )
    logger.debug("original: %s...", hypothesis.text[:100])
    if nearest is not None:
        logger.debug("similar to: %s...", nearest.text[:100])
    return None


def _apply_evolution_result(
    hypothesis: Hypothesis,
    response: dict[str, Any],
    peers: list[Hypothesis],
    context: EvolutionContext,
    operation: _EvolutionOperation,
) -> tuple[Hypothesis | None, dict[str, Any] | None]:
    fields = _extract_evolution_fields(hypothesis, response)
    if _rejected_as_unchanged(hypothesis, fields.refined_text):
        return None, None
    parents = _resolve_parents(hypothesis, response, operation.partners)
    max_similarity = _near_duplicate_similarity(
        hypothesis,
        fields.refined_text,
        peers,
        context.proximity_graph,
        frozenset(parent.id for parent in parents[1:]),
    )
    if max_similarity is None:
        return None, None
    child, detail = _apply_refined_hypothesis(parents, fields, max_similarity, context)
    detail["operator"] = str(response.get("_evolution_operator") or "enhancement")
    return child, detail


def _collect_evolution_results(
    results: list[tuple[Hypothesis | None, dict[str, Any] | None]],
) -> tuple[list[Hypothesis], list[dict[str, Any]]]:
    children: list[Hypothesis] = []
    evolution_details: list[dict[str, Any]] = []

    for child, detail in results:
        if child is not None:
            children.append(child)
        if detail is not None:
            evolution_details.append(detail)

    return children, evolution_details


def _build_evolve_state_delta(
    children: list[Hypothesis],
    evolution_details: list[dict[str, Any]],
    llm_call_count: int,
) -> dict[str, Any]:
    """Bill every parent attempt and enhancement query call, not just
    accepted children."""
    metrics = create_metrics_update(
        deltas=MetricDeltas(llm_calls=llm_call_count, evolutions=len(children))
    )
    logger.debug(
        "evolve node creating metrics delta: children=%s, llm_calls=%s",
        len(children),
        llm_call_count,
    )

    # AppendHypotheses retains all parents and peers instead of replacing the
    # pool.
    return {
        "hypotheses": AppendHypotheses(children),
        "evolution_details": evolution_details,
        "metrics": metrics,
        "messages": phase_message(
            "evolve",
            f"Evolved {len(children)} new child hypotheses",
            evolved_count=len(children),
        ),
    }
