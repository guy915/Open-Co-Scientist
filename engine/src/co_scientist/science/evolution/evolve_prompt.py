from __future__ import annotations

import dataclasses
import enum
import json
import logging
import random
from collections.abc import Mapping, Sequence
from typing import Any

from co_scientist.core.prompt_layout import append_item_context
from co_scientist.domains.research_state.models import Hypothesis, rank_by_elo
from co_scientist.domains.research_state.proximity_edges import is_judged_edge
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.domains.research_state.text_utils import token_coverage
from co_scientist.science.evolution.evolve_grounding import (
    not_applicable_block,
)
from co_scientist.science.evolution.operations import EvolutionContext
from co_scientist.science.falsified_assumptions import build_falsified_assumptions_section
from co_scientist.science.prompts import (
    format_lab_constraints_section,
    format_preferences,
    load_prompt_with_schema,
)
from co_scientist.science.prompts._common import (
    _csv_value,
    _format_bullet_list,
    _format_run_guidance,
)
from co_scientist.science.prompts.context_budget import (
    compact_json_context,
    select_evidence_excerpt,
    summarize_references,
)
from co_scientist.science.prompts.generation_draft import (
    _build_citation_reference_section,
)
from co_scientist.science.prompts.loading import _get_domain_variables
from co_scientist.science.review_summary import mature_review_summary

logger = logging.getLogger(__name__)


class EvolutionOperator(str, enum.Enum):
    ENHANCEMENT = "enhancement"
    COHERENCE_FEASIBILITY = "coherence_feasibility"
    INSPIRATION = "inspiration"
    COMBINATION = "combination"
    SIMPLIFICATION = "simplification"
    ANALOGY = "analogy"
    OUT_OF_BOX = "out_of_box"


_INSTRUCTIONS = {
    EvolutionOperator.ENHANCEMENT: (
        "Strengthen the hypothesis's grounding in evidence: identify its "
        "weaknesses and reasoning gaps, and elaborate details using the "
        "targeted literature supplied for this refinement, while retaining "
        "the scientifically valuable premise."
    ),
    EvolutionOperator.INSPIRATION: (
        "Evolve the idea by borrowing the mechanism or structure of one of "
        "the existing top-ranked approaches supplied as partners into this "
        "hypothesis's target context. State what was borrowed, from which "
        "approach, and what was adapted rather than replicated."
    ),
    EvolutionOperator.COMBINATION: (
        "Synthesize complementary mechanisms or experiments from the parent "
        "and the combination partners supplied. Combination is required; do "
        "not merely polish the parent in isolation."
    ),
    EvolutionOperator.SIMPLIFICATION: (
        "Remove unnecessary assumptions and experimental complexity. Produce "
        "the smallest mechanism and decisive experiment that can test it."
    ),
    EvolutionOperator.ANALOGY: (
        "Transfer a defensible mechanism or experimental pattern from a "
        "different biological system or scientific domain, state the mapping, "
        "and identify where the analogy could fail."
    ),
}

_TEMPLATES = {
    EvolutionOperator.COHERENCE_FEASIBILITY: "evolution_feasibility",
    EvolutionOperator.OUT_OF_BOX: "evolution_out_of_box",
}


def operator_instruction(operator: EvolutionOperator) -> str:
    return _INSTRUCTIONS[operator]


def operator_template(operator: EvolutionOperator) -> str:
    return _TEMPLATES.get(operator, "evolution")


def select_operators(count: int, iteration: int, seed_material: str) -> list[EvolutionOperator]:
    """Seeded shuffled decks cover all operators across small-parent rounds;
    simple index/iteration cycling can permanently skip some."""
    if count <= 0:
        return []
    deck = list(EvolutionOperator)
    rng = random.Random(f"{seed_material}:evolution-operators")
    rng.shuffle(deck)
    offset = (iteration * count) % len(deck)
    return [deck[(offset + index) % len(deck)] for index in range(count)]


def _debates_for(state: WorkflowState, hypothesis: Hypothesis) -> list[dict[str, Any]]:
    return [
        {
            "debate_id": item.get("debate_id"),
            "transcript": str(item.get("transcript") or "")[-2500:],
        }
        for item in state.get("debate_transcripts") or []
        if item.get("hypothesis_text") == hypothesis.text
    ][-2:]


def _tournament_matches_for(state: WorkflowState, hypothesis: Hypothesis) -> list[dict[str, Any]]:
    matches = []
    for item in state.get("tournament_matchups", []):
        side_a = item.get("hypothesis_a_id") == hypothesis.id
        side_b = item.get("hypothesis_b_id") == hypothesis.id
        if not side_a and not side_b:
            continue
        matches.append(
            {
                "outcome": ("won" if item.get("winner_id") == hypothesis.id else "lost"),
                "reasoning": item.get("reasoning") or item.get("reason"),
                "confidence": item.get("confidence"),
            }
        )
    return matches


def _proximity_neighbors_for(state: WorkflowState, hypothesis: Hypothesis) -> list[dict[str, Any]]:
    """The graph can exceed this ledger cap; sort by similarity so the
    retained context names nearest neighbors, not first-listed pairs."""
    neighbors = []
    for edge in (state.get("proximity_graph") or {}).get("edges", []):
        if edge.get("source") == hypothesis.id:
            neighbor = edge.get("target")
        elif edge.get("target") == hypothesis.id:
            neighbor = edge.get("source")
        else:
            continue
        neighbors.append(
            {
                "hypothesis_id": neighbor,
                "similarity": edge.get("similarity"),
                "cluster_id": edge.get("cluster_id"),
            }
        )
    neighbors.sort(key=lambda n: float(n["similarity"] or 0.0), reverse=True)
    return neighbors


def _compact_specialist_records(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _compact_specialist_records(item) for key, item in value.items()}
    if not isinstance(value, list):
        return value
    children = [_compact_specialist_records(item) for item in value]
    if (
        len(value) > 1
        and isinstance(value[0], dict)
        and value[0]
        and all(isinstance(item, dict) and item.keys() == value[0].keys() for item in value)
    ):
        columns = list(value[0])
        return {"columns": columns, "rows": [[item[key] for key in columns] for item in children]}
    return children


def _specialist_feedback_for(state: WorkflowState, hypothesis: Hypothesis) -> str:
    ledger: dict[str, Any] = {
        "claim_evidence_gate": hypothesis.enrichments.get("claim_gate") or {},
        "debates": _debates_for(state, hypothesis),
        "tournament": _tournament_matches_for(state, hypothesis)[-8:],
        "proximity_neighbors": _proximity_neighbors_for(state, hypothesis)[:8],
    }
    # A hollow verification block implies checked/no findings; omit it when
    # verification has never run.
    deep_verification = hypothesis.deep_verification_summary()
    if deep_verification is not None:
        ledger["deep_verification"] = deep_verification
    mature_reviews = mature_review_summary(hypothesis.enrichments)
    if mature_reviews is not None:
        ledger["mature_reviews"] = mature_reviews
    return compact_json_context(json.dumps(_compact_specialist_records(ledger)), 4_000)


def _sample_up_to(pool: list[Hypothesis], count: int, rng: random.Random) -> list[Hypothesis]:
    """Use caller RNG so concurrent runs cannot perturb draws; empty pools
    consume no RNG state."""
    if not pool:
        return []
    return rng.sample(pool, min(count, len(pool)))


def _sample_top_and_random(
    others_by_elo: list[Hypothesis],
    top_count: int,
    random_count: int,
    rng: random.Random,
) -> list[Hypothesis]:
    top_performers = others_by_elo[:top_count]
    remaining = others_by_elo[top_count:]
    sampled_others = _sample_up_to(remaining, random_count, rng)

    logger.debug(
        "sampled %s context hypotheses (top %s + %s random) from %s total",
        len(top_performers) + len(sampled_others),
        top_count,
        len(sampled_others),
        len(others_by_elo),
    )

    return top_performers + sampled_others


def sample_context_hypotheses(
    all_hypotheses: list[Hypothesis],
    exclude_hypothesis: Hypothesis,
    max_context: int = 15,
    ranked_hypotheses: list[Hypothesis] | None = None,
    rng: random.Random | None = None,
) -> list[Hypothesis]:
    others = [h for h in all_hypotheses if h.text != exclude_hypothesis.text]

    if len(others) <= max_context:
        return others

    if ranked_hypotheses is None:
        others_by_elo = rank_by_elo(others)
    else:
        others_by_elo = [h for h in ranked_hypotheses if h.text != exclude_hypothesis.text]

    draw = rng if rng is not None else random.Random()
    return _sample_top_and_random(others_by_elo, 5, 10, draw)


def combination_partners(
    ranked_hypotheses: Sequence[Hypothesis],
    parent: Hypothesis,
    max_partners: int = 2,
) -> list[Hypothesis]:
    partners = [h for h in ranked_hypotheses if h.text != parent.text]
    return partners[:max_partners]


def proximity_weights_for(
    proximity_graph: dict[str, Any] | None, hypothesis_id: str
) -> dict[str, float]:
    """Computed parent edges cannot measure a new child; use judged edges
    only, then directly measure child text for fallback."""
    weights: dict[str, float] = {}
    for edge in (proximity_graph or {}).get("edges", []):
        if not is_judged_edge(edge):
            continue
        source = edge.get("source")
        target = edge.get("target")
        similarity = edge.get("similarity")
        if source == hypothesis_id and target is not None:
            weights[target] = float(similarity or 0.0)
        elif target == hypothesis_id and source is not None:
            weights[source] = float(similarity or 0.0)
    return weights


def find_nearest_peer(
    refined_text: str,
    parent_id: str,
    peers: list[Hypothesis],
    proximity_graph: dict[str, Any] | None = None,
) -> tuple[float, Hypothesis | None]:
    """Use parent-neighbor model judgments where available; token coverage
    measures the child directly when no judged semantic edge exists."""
    weights = proximity_weights_for(proximity_graph, parent_id)
    max_similarity = 0.0
    nearest: Hypothesis | None = None
    for peer in peers:
        weight = weights.get(peer.id)
        similarity = weight if weight is not None else token_coverage(refined_text, peer.text)
        if similarity > max_similarity:
            max_similarity = similarity
            nearest = peer
    return max_similarity, nearest


_PARTNER_SECTION_HEADERS = {
    EvolutionOperator.COMBINATION: (
        "## Combination Partners\n"
        "These top-ranked hypotheses are designated to be merged with the "
        "parent. Their full fields follow, untruncated. Identify any partner "
        "you merge by its positional index in your response; never echo a "
        "partner's text back."
    ),
    EvolutionOperator.INSPIRATION: (
        "## Inspiration Sources\n"
        "These top-ranked hypotheses are existing approaches you may borrow "
        "mechanism or structure from. Their full fields follow, untruncated."
    ),
    EvolutionOperator.OUT_OF_BOX: "## Provided Concepts",
}


def _format_partner_context(partners: tuple[Hypothesis, ...], operator: EvolutionOperator) -> str:
    """Combining actual mechanisms needs whole partner fields, not snippets."""
    header = _PARTNER_SECTION_HEADERS.get(operator)
    if header is None:
        return "(No partners are assigned to this operator.)"
    if not partners:
        return (
            f"{header}\n\n"
            "(No partners are available in this pool; work from the parent "
            "and the context already provided.)"
        )
    sections = [header]
    for index, partner in enumerate(partners, start=1):
        sections.append(
            f"### Partner {index}\n"
            f"**Hypothesis:** {partner.text}\n"
            f"**Explanation:** {partner.explanation or 'Not provided.'}\n"
            f"**Mechanism grounding:** "
            f"{partner.literature_grounding or 'Not provided.'}\n"
            f"**Experiment:** {partner.experiment or 'Not provided.'}"
        )
    return "\n\n".join(sections)


def _format_operator_section(operator: EvolutionOperator) -> str:
    return (
        "\n\n## Required Evolution Operator\n"
        f"**Operator:** {operator.value}\n"
        f"{operator_instruction(operator)}\n"
        "Record how this operator changed the proposal in the refinement "
        "summary.\n"
    )


def render_operator_template(
    operator: EvolutionOperator,
    variables: dict[str, Any],
    diversity: str,
) -> tuple[str, dict[str, Any] | None, str, bool]:
    template = operator_template(operator)
    has_template_diversity_slot = template != "evolution"
    if has_template_diversity_slot:
        # Published templates already contain the role and terminal response
        # cue.
        variables["diversity_section"] = diversity
        prompt, schema = load_prompt_with_schema(template, variables)
        operator_section = ""
    else:
        prompt, schema = load_prompt_with_schema(template, variables)
        operator_section = _format_operator_section(operator)
    return prompt, schema, operator_section, has_template_diversity_slot


@dataclasses.dataclass(frozen=True)
class _EvolutionOperation:
    """Prompt partner context and recorded combination lineage must resolve
    against the same partner list."""

    operator: EvolutionOperator = EvolutionOperator.ENHANCEMENT
    specialist_feedback: str = ""
    partners: tuple[Hypothesis, ...] = ()


def _build_review_feedback(hypothesis: Hypothesis) -> str:
    summary = hypothesis.review_summary()
    if summary is None:
        return ""
    return json.dumps(summary, indent=2)


def _build_supervisor_guidance_text(
    supervisor_guidance: dict[str, Any] | None,
) -> str:
    if not supervisor_guidance or not isinstance(supervisor_guidance, dict):
        return ""
    phase = supervisor_guidance.get("workflow_plan", {}).get("evolution_phase", {})
    if not phase:
        return ""
    sections = ["## Supervisor Guidance for Evolution\n"]
    if phase.get("refinement_priorities"):
        sections.append(
            f"**Refinement Priorities:** {_csv_value(phase['refinement_priorities'])}\n"
        )
    if phase.get("iteration_strategy"):
        sections.append(f"**Iteration Strategy:** {phase['iteration_strategy']}\n")
    sections.append("\nUse this guidance to align your refinement with the research plan.\n")
    return "".join(sections)


_DIVERSITY_INSTRUCTION_TEMPLATE = """

## CRITICAL: Preserve Diversity

**Other hypotheses in the active pool:**
{other_hyps}

**Previously removed duplicates (DO NOT recreate these):**
{removed_dups}

**CRITICAL REQUIREMENT:** Your refined hypothesis MUST remain DISTINCT from:
1. All other hypotheses listed above
2. Previously removed duplicates

DO NOT:
- Use the same biomarker/methodology as other hypotheses
- Make only trivial wording changes
- Converge toward similar concepts

DO:
- Maintain the unique aspects of this hypothesis
- Explore different mechanisms or approaches
- Preserve conceptual diversity
"""

# A combination legitimately shares partner mechanisms; anti-convergence exempts
# those partners while still rejecting other peers and pruned duplicates.
_COMBINATION_DIVERSITY_TEMPLATE = """

## CRITICAL: Preserve Diversity

**Other hypotheses in the active pool:**
{other_hyps}

**Previously removed duplicates (DO NOT recreate these):**
{removed_dups}

**CRITICAL REQUIREMENT:** Your combination MUST synthesize the designated
combination partners with the parent; sharing mechanisms with those partners
is the point of this operator. The result must still:
1. Remain DISTINCT from every other hypothesis listed above that is not a
   designated partner
2. NOT recreate any previously removed duplicate

DO NOT make only trivial wording changes; the synthesis must resolve the
parents' weaknesses, not restate them.
"""


def _format_diversity_instruction(
    other_hypotheses_texts: list[str],
    removed_duplicates: list[str],
    operator: EvolutionOperator = EvolutionOperator.ENHANCEMENT,
) -> str:
    peer_chars = min(200, max(0, 6_000 // max(1, len(other_hypotheses_texts)) - 6))
    other_hyps_formatted = _format_bullet_list(other_hypotheses_texts, truncate_chars=peer_chars)
    removed_dups_formatted = _format_bullet_list(removed_duplicates[-5:], truncate_chars=200)

    template = (
        _COMBINATION_DIVERSITY_TEMPLATE
        if operator is EvolutionOperator.COMBINATION
        else _DIVERSITY_INSTRUCTION_TEMPLATE
    )
    return template.format(
        other_hyps=other_hyps_formatted,
        removed_dups=removed_dups_formatted,
    )


def _build_evolution_variables(
    hypothesis: Hypothesis,
    context: EvolutionContext,
    operation: _EvolutionOperation,
    grounding_evidence: str,
) -> dict[str, Any]:
    variables = _base_evolution_variables(
        hypothesis, context.meta_review, context.supervisor_guidance
    )
    variables["run_guidance"] = _format_run_guidance(
        context.run_setup_guidance, context.run_focus_guidance
    )
    state: Mapping[str, Any] = context.state or {}
    variables["articles_with_reasoning"] = select_evidence_excerpt(
        context.articles_with_reasoning or "", state.get("research_goal", ""), 6_000
    )
    # The schema permits only supplied C* keys; no reference index would force a
    # child to disclaim grounding or invent unresolvable citations.
    variables["citation_reference_section"] = _build_citation_reference_section(
        summarize_references(context.reference_index.text) if context.reference_index else ""
    )
    variables["enhancement_grounding"] = select_evidence_excerpt(
        grounding_evidence, hypothesis.text, 6_000
    )
    variables["partner_context"] = _format_partner_context(operation.partners, operation.operator)
    variables["falsified_assumptions_section"] = build_falsified_assumptions_section(
        state.get("hypotheses")
    )
    variables["lab_constraints_section"] = format_lab_constraints_section(
        state.get("lab_constraints")
    )
    variables["research_goal"] = state.get("research_goal") or ""
    variables["preferences"] = format_preferences(state.get("preferences"))
    variables["specialist_feedback"] = (
        operation.specialist_feedback or "No prior specialist feedback."
    )
    variables.update(_get_domain_variables(context.tool_registry))
    return variables


def _base_evolution_variables(
    hypothesis: Hypothesis,
    meta_review: dict[str, Any],
    supervisor_guidance: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "original_hypothesis": hypothesis.text,
        "review_feedback": compact_json_context(_build_review_feedback(hypothesis), 3_000),
        "meta_review_insights": compact_json_context(
            json.dumps(
                {
                    "common_strengths": meta_review.get("common_strengths", []),
                    "common_weaknesses": meta_review.get("common_weaknesses", []),
                    "strategic_recommendations": meta_review.get("strategic_recommendations", []),
                    "emerging_themes": meta_review.get("emerging_themes", []),
                },
                indent=2,
            ),
            3_000,
        ),
        "supervisor_guidance": _build_supervisor_guidance_text(supervisor_guidance),
    }


def _build_evolution_prompt(
    hypothesis: Hypothesis,
    other_hypotheses_texts: list[str],
    context: EvolutionContext,
    operation: _EvolutionOperation,
    grounding_evidence: str = "",
) -> tuple[str, dict[str, Any] | None]:
    variables = _build_evolution_variables(
        hypothesis,
        context,
        operation,
        grounding_evidence or not_applicable_block(),
    )
    diversity = _format_diversity_instruction(
        other_hypotheses_texts,
        context.removed_duplicates,
        operation.operator,
    )
    prompt, schema, operator_section, has_template_diversity_slot = render_operator_template(
        operation.operator, variables, diversity
    )
    if not has_template_diversity_slot:
        operator_section += diversity
    return append_item_context(prompt, operator_section), schema
