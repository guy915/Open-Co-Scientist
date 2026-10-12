import dataclasses
from typing import Any

from co_scientist.domains.research_state.models import Hypothesis, rank_by_elo
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science.citations import (
    ReferenceIndex,
    build_reference_index,
)


@dataclasses.dataclass(frozen=True)
class EvolutionContext:
    """Prompt keys and child citations use one reference index; round-ranked
    context stays stable while processing parents."""

    model_name: str
    meta_review: dict[str, Any]
    removed_duplicates: list[str]
    creation_iteration: int | None = None
    supervisor_guidance: dict[str, Any] | None = None
    articles_with_reasoning: str | None = None
    run_id: str | None = None
    tool_registry: Any | None = None
    run_setup_guidance: str | None = None
    run_focus_guidance: str | None = None
    proximity_graph: dict[str, Any] | None = None
    ranked_hypotheses: tuple[Hypothesis, ...] = ()
    state: WorkflowState | None = None
    reference_index: ReferenceIndex | None = None


def build_evolution_context(
    state: WorkflowState,
    removed_duplicates: list[str],
    supervisor_guidance: dict[str, Any] | None,
) -> EvolutionContext:
    return EvolutionContext(
        model_name=state["model_name"],
        meta_review=state.get("meta_review", {}),
        removed_duplicates=removed_duplicates,
        creation_iteration=state.get("current_iteration", 0),
        supervisor_guidance=supervisor_guidance,
        articles_with_reasoning=state.get("articles_with_reasoning"),
        run_id=state.get("run_id"),
        tool_registry=state.get("tool_registry"),
        run_setup_guidance=state.get("run_setup_guidance"),
        run_focus_guidance=state.get("run_focus_guidance"),
        proximity_graph=state.get("proximity_graph"),
        ranked_hypotheses=tuple(rank_by_elo(state["hypotheses"])),
        state=state,
        # Build one C* index per round so prompt keys and all child resolutions
        # agree.
        reference_index=build_reference_index(
            state.get("articles"), state.get("context_enrichment_sources")
        ),
    )
