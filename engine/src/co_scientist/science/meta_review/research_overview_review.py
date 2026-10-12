"""Review scientific accuracy against the run's material, not prose style. At
the round cap, publish the last revision without another unusable verdict."""

from dataclasses import dataclass
from typing import Any, Final

from co_scientist.core.constants import (
    MEDIUM_TEMPERATURE,
    RESEARCH_OVERVIEW_MAX_TOKENS,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    call_llm_json,
    scoped_telemetry_phase,
)
from co_scientist.science.prompts import (
    OverviewReviewMaterial,
    OverviewRevisionRequest,
    get_research_overview_review_prompt,
    get_research_overview_revise_prompt,
)

_MAX_OVERVIEW_REVISION_ROUNDS: Final = 2
# The last revision publishes at the cap; another verdict cannot change its
# outcome.


@dataclass(frozen=True)
class OverviewReviewContext:
    state: WorkflowState
    research_goal: str
    hypotheses_summary: str
    contact_candidates_text: str
    evidence_corpus_text: str

    @property
    def material(self) -> OverviewReviewMaterial:
        return OverviewReviewMaterial(
            research_goal=self.research_goal,
            hypotheses_summary=self.hypotheses_summary,
            evidence_corpus=self.evidence_corpus_text,
        )


async def review_research_overview(
    context: OverviewReviewContext, draft_response: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], int]:
    current = draft_response
    calls = 0
    for round_index in range(_MAX_OVERVIEW_REVISION_ROUNDS):
        verdict = await _call_reviewer(context, current)
        calls += 1
        if verdict.get("accept", True):
            return current, _review_meta(round_index), calls
        current = await _call_reviser(context, current, verdict.get("notes") or [])
        calls += 1
    return current, _review_meta(_MAX_OVERVIEW_REVISION_ROUNDS), calls


def _review_meta(rounds: int) -> dict[str, Any]:
    return {"reviewed": rounds > 0, "rounds": rounds}


async def _call_supervisor(
    context: OverviewReviewContext,
    prompt: str,
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Separate telemetry keeps accuracy-review spend distinguishable from
    draft spend."""
    with scoped_telemetry_phase("review"):
        return await call_llm_json(
            prompt=prompt,
            spec=CompletionSpec(
                role="overview_review",
                model_name=context.state["supervisor_model_name"],
                max_tokens=RESEARCH_OVERVIEW_MAX_TOKENS,
                temperature=MEDIUM_TEMPERATURE,
                json_schema=schema,
            ),
        )


async def _call_reviewer(
    context: OverviewReviewContext, draft_response: dict[str, Any]
) -> dict[str, Any]:
    prompt, schema = get_research_overview_review_prompt(
        context.material, _format_overview_for_review(draft_response)
    )
    return await _call_supervisor(context, prompt, schema)


async def _call_reviser(
    context: OverviewReviewContext,
    draft_response: dict[str, Any],
    notes: list[Any],
) -> dict[str, Any]:
    prompt, schema = get_research_overview_revise_prompt(
        OverviewRevisionRequest(
            material=context.material,
            contact_candidates=context.contact_candidates_text,
            drafted_overview=_format_overview_for_review(draft_response),
            review_notes=_format_review_notes(notes),
        )
    )
    return await _call_supervisor(context, prompt, schema)


def _format_overview_for_review(response: dict[str, Any]) -> str:
    overview = response.get("overview") or {}
    aims = response.get("nih_specific_aims") or {}
    lines = [f"Summary: {overview.get('summary', '')}"]
    for index, direction in enumerate(overview.get("research_directions") or []):
        lines.append(
            f"Direction {index + 1} - {direction.get('title', '')}: "
            f"{direction.get('importance', '')}"
        )
    lines.append(f"Disease description: {aims.get('disease_description', '')}")
    lines.append(f"Unmet need: {aims.get('unmet_need', '')}")
    lines.append(f"Proposed solution: {aims.get('proposed_solution', '')}")
    for index, aim in enumerate(aims.get("aims") or []):
        lines.append(
            f"Aim {index + 1} - {aim.get('overarching_goal', '')}: {aim.get('hypothesis', '')}"
        )
    lines.append(f"Pilot evaluation: {aims.get('pilot_evaluation', '')}")
    lines.extend(_format_knowledge_base_for_review(response))
    return "\n".join(lines)


def _format_knowledge_base_for_review(response: dict[str, Any]) -> list[str]:
    lines = []
    for topic in response.get("knowledge_base") or []:
        evidence_ids = ", ".join(topic.get("evidence_ids") or [])
        lines.append(
            f"Knowledge-base topic '{topic.get('title', '')}' "
            f"(evidence: {evidence_ids}): {topic.get('summary', '')} "
            f"{topic.get('detail', '')}"
        )
    return lines


def _format_review_notes(notes: list[Any]) -> str:
    lines = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        evidence_id = note.get("evidence_id")
        suffix = f" (evidence_id: {evidence_id})" if evidence_id else ""
        lines.append(f"- {note.get('location', '')}: {note.get('issue', '')}{suffix}")
    return "\n".join(lines) if lines else "No specific notes."
