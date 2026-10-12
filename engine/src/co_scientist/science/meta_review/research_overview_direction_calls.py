from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from co_scientist.core.constants import (
    MEDIUM_TEMPERATURE,
    RESEARCH_OVERVIEW_DIRECTION_MAX_TOKENS,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    scoped_telemetry_phase,
)
from co_scientist.science.meta_review.research_overview_evidence import (
    prompt_context,
)
from co_scientist.science.prompts import (
    DirectionWritingMaterial,
    get_research_overview_direction_prompt,
)
from co_scientist.science.schemas.synthesis import (
    RESEARCH_OVERVIEW_MAX_DIRECTIONS,
    RESEARCH_OVERVIEW_MAX_SUB_TOPIC_QUESTIONS,
    RESEARCH_OVERVIEW_MAX_SUB_TOPICS,
)

logger = logging.getLogger(__name__)


def _validate_specific_questions(raw_questions: Any) -> list[Any]:
    if not isinstance(raw_questions, list):
        return []
    return raw_questions[:RESEARCH_OVERVIEW_MAX_SUB_TOPIC_QUESTIONS]


def _validate_sub_topic(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "title": raw.get("title") or "",
        "why": raw.get("why") or "",
        "what": raw.get("what") or "",
        # json_object can omit required examples; preserve the sub-topic without
        # one rather than failing the whole response.
        "example_idea": raw.get("example_idea") or "",
        "specific_questions": _validate_specific_questions(raw.get("specific_questions")),
    }


def _validate_sub_topics(raw_sub_topics: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_sub_topics, list):
        return []
    sliced = raw_sub_topics[:RESEARCH_OVERVIEW_MAX_SUB_TOPICS]
    formatted = (_validate_sub_topic(raw) for raw in sliced)
    return [sub_topic for sub_topic in formatted if sub_topic is not None]


def _validate_research_direction(raw: Any) -> Any:
    """Only new fields are validated here; existing malformed/pass-through
    shapes retain their renderer-owned behavior."""
    if not isinstance(raw, dict):
        return raw
    return {
        **raw,
        "recent_findings": raw.get("recent_findings") or "",
        "sub_topics": _validate_sub_topics(raw.get("sub_topics")),
    }


def _validate_research_directions(raw_directions: Any) -> Any:
    """json_object ignores maxItems; enforce caps rather than trusting an
    oversized response already at the budget escalation ceiling."""
    if not isinstance(raw_directions, list):
        return raw_directions
    sliced = raw_directions[:RESEARCH_OVERVIEW_MAX_DIRECTIONS]
    return [_validate_research_direction(raw) for raw in sliced]


def format_overview(raw_overview: Any) -> Any:
    """Touch direction fields only when present; preserve missing/malformed
    overview and all established pass-through fields."""
    has_directions = isinstance(raw_overview, dict) and "research_directions" in raw_overview
    if not has_directions:
        return raw_overview
    return {
        **raw_overview,
        "research_directions": _validate_research_directions(raw_overview["research_directions"]),
    }


JsonCall = Callable[..., Awaitable[dict[str, Any]]]

# One injected call seam keeps draft, review and direction waves from bypassing
# caller stubs.


_BODY_FIELDS = (
    "importance",
    "recent_findings",
    "suggested_experiments",
    "sub_topics",
)

# Titles are contact/group cross-reference keys and must not be overwritten;
# undeclared body keys cannot reach the report.


@dataclass(frozen=True)
class DirectionWaveContext:
    state: WorkflowState
    hypotheses_summary: str
    evidence_corpus_text: str


def _needs_writing(direction: Any) -> bool:
    """A schema-required placeholder is not a developed direction; all body
    fields must be present before skipping its writing call."""
    if not isinstance(direction, dict):
        return False
    return not all(direction.get(field) for field in _BODY_FIELDS)


async def write_direction_body(
    context: DirectionWaveContext,
    material: DirectionWritingMaterial,
    ask: JsonCall,
) -> dict[str, Any]:
    """Direction failure must not abort valid peers; an unwritten body leaves
    its drafted title and argument publishable."""
    prompt, schema = get_research_overview_direction_prompt(
        research_goal=context.state["research_goal"],
        material=material,
        hypotheses_summary=context.hypotheses_summary,
        evidence_corpus=context.evidence_corpus_text,
        context=prompt_context(context.state),
    )
    try:
        with scoped_telemetry_phase("directions"):
            response = await ask(
                prompt=prompt,
                spec=CompletionSpec(
                    role="overview_directions",
                    model_name=context.state["supervisor_model_name"],
                    max_tokens=RESEARCH_OVERVIEW_DIRECTION_MAX_TOKENS,
                    temperature=MEDIUM_TEMPERATURE,
                    json_schema=schema,
                ),
            )
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception:
        logger.error(
            "Research direction %r was not developed; publishing its "
            "drafted title and argument alone",
            material.title,
            exc_info=True,
        )
        return {}
    return response if isinstance(response, dict) else {}


def _merged(direction: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Lax providers may omit required fields; only answered body fields
    replace the draft so one missing field cannot erase the direction."""
    written = {
        field: body[field] for field in _BODY_FIELDS if isinstance(body, dict) and body.get(field)
    }
    return {**direction, **written}


async def develop_research_directions(
    context: DirectionWaveContext, directions: Any, ask: JsonCall
) -> tuple[Any, int]:
    if not isinstance(directions, list):
        return directions, 0
    pending = [index for index, d in enumerate(directions) if _needs_writing(d)]
    if not pending:
        return directions, 0
    titles = "\n".join(
        f"- {str(direction.get('title') or '').strip()}"
        for direction in directions
        if isinstance(direction, dict)
    )
    bodies = await asyncio.gather(
        *(
            write_direction_body(
                context,
                DirectionWritingMaterial(
                    title=str(directions[index].get("title") or "").strip(),
                    rationale=str(directions[index].get("importance") or "").strip(),
                    all_directions=titles,
                ),
                ask,
            )
            for index in pending
        )
    )
    written = dict(zip(pending, bodies, strict=True))
    return [
        _merged(direction, written[index]) if index in written else direction
        for index, direction in enumerate(directions)
    ], len(pending)


async def develop_directions_into(
    wave: DirectionWaveContext, formatted: dict[str, Any], ask: JsonCall
) -> int:
    """Six deep bodies cannot fit one provider clock; cap per-direction calls
    and revalidate the merged output."""
    overview = formatted.get("overview")
    if not isinstance(overview, dict):
        return 0
    developed, calls = await develop_research_directions(
        wave, overview.get("research_directions"), ask
    )
    formatted["overview"] = format_overview({**overview, "research_directions": developed})
    return calls
