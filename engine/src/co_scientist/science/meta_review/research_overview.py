from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final

from co_scientist.core.constants import (
    MEDIUM_TEMPERATURE,
    PROGRESS_RESEARCH_OVERVIEW_COMPLETE,
    PROGRESS_RESEARCH_OVERVIEW_START,
    RESEARCH_OVERVIEW_INTERIM_MAX_TOKENS,
    RESEARCH_OVERVIEW_MAX_TOKENS,
    RESEARCH_OVERVIEW_TOP_K,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS, short_error_text
from co_scientist.domains.research_state.models import (
    Hypothesis,
    MetricDeltas,
    create_metrics_update,
    phase_message,
    rank_for_publication,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.domains.safety.rules import is_blocking_status
from co_scientist.platform.llm import (
    CompletionSpec,
    call_llm_json,
)
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.meta_review.research_overview_direction_calls import (
    DirectionWaveContext,
    develop_directions_into,
    format_overview,
)
from co_scientist.science.meta_review.research_overview_evidence import (
    _build_contact_candidates,
    _format_contact_candidates,
    _format_evidence_corpus,
    _validate_research_contact_groups,
    _validate_research_contacts,
)
from co_scientist.science.meta_review.research_overview_evidence import (
    _build_evidence_corpus as _build_evidence_corpus,
)
from co_scientist.science.meta_review.research_overview_knowledge_base import (
    _validate_knowledge_base,
    knowledge_base_is_funded,
)
from co_scientist.science.meta_review.research_overview_knowledge_base import (
    synthesize_knowledge_base as synthesize_knowledge_base,
)
from co_scientist.science.meta_review.research_overview_review import (
    OverviewReviewContext as OverviewReviewContext,
)
from co_scientist.science.meta_review.research_overview_review import (
    review_research_overview as review_research_overview,
)
from co_scientist.science.node_degradation import run_or_degrade
from co_scientist.science.prompts import (
    PromptRunContext,
    get_research_overview_interim_prompt,
    get_research_overview_prompt,
)
from co_scientist.science.scheduling.models import TaskType, stacked_task_values
from co_scientist.science.schemas.synthesis import (
    RESEARCH_OVERVIEW_INTERIM_MAX_DIRECTIONS as _MAX_DIRECTIONS,
)
from co_scientist.science.schemas.synthesis import (
    RESEARCH_OVERVIEW_INTERIM_MAX_QUESTIONS as _MAX_QUESTIONS,
)

logger = logging.getLogger(__name__)


_OVERVIEW_SCHEMA = "research_overview"

_LOST = (
    "publishing the report without the overview, Specific Aims, knowledge "
    "base and research contacts"
)


def is_interim_firing(state: WorkflowState) -> bool:
    """Stacked companion firings keep another primary in next_task; read
    queue actions too or an interim pass buys and publishes terminal work."""
    if str(state.get("next_task") or "") == TaskType.SYNTHESIZE.value:
        return True
    actions = state.get("supervisor_queue_actions") or []
    return TaskType.SYNTHESIZE.value in stacked_task_values(actions)


async def synthesize_or_degrade(
    state: WorkflowState,
    synthesize: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    if is_interim_firing(state):
        return await _interim_or_degrade(state, synthesize)
    return await run_or_degrade(
        state,
        synthesize,
        schema_name=_OVERVIEW_SCHEMA,
        fallback=_degraded_overview_result,
        lost=_LOST,
    )


async def _interim_or_degrade(
    state: WorkflowState,
    synthesize: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Interim output is not a report section, so do not mark degradation;
    declined retries still spend the shared task attempt budget."""
    try:
        return await synthesize()
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        if state.get("durable_retries_remain"):
            raise
        logger.error(
            "Interim research overview could not reach the provider (%s); "
            "the next generate cycle runs without one",
            short_error_text(exc),
        )
        return {}


def _degraded_overview_result() -> dict[str, Any]:
    """Failed provider requests were already billed; an empty reportable
    section must not attribute another successful synthesis call."""
    return {
        "research_overview": {},
        "messages": phase_message(
            "research_overview",
            "Research overview synthesis could not reach the provider; "
            "the report is published without it",
        ),
    }


# Share caps with the interim schema so the model is not asked to produce titles
# or questions that this renderer would discard.

_HEADER: Final = (
    "## Interim research overview (this run's own synthesis so far)\n\n"
    "The system synthesized the ideas produced so far into the directions"
    " and open questions below. Push into what they leave open: prefer a"
    " mechanism, model system or intervention these do not already cover,"
    " and do not re-derive a direction already named here.\n"
)


def build_interim_overview(response: dict[str, Any]) -> str:
    overview = response.get("overview")
    directions = overview.get("research_directions") if isinstance(overview, dict) else None
    lines = _titled_lines(directions, "Directions", _MAX_DIRECTIONS)
    lines += _question_lines(response.get("open_questions"))
    return f"{_HEADER}\n" + "\n".join(lines) + "\n" if lines else ""


def _titled_lines(raw: Any, header: str, limit: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    bullets = [
        f"- {str(item.get('title') or '').strip()}"
        for item in raw[:limit]
        if isinstance(item, dict) and str(item.get("title") or "").strip()
    ]
    return [f"**{header}:**", *bullets, ""] if bullets else []


def _question_lines(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    bullets = [f"- {str(item).strip()}" for item in raw[:_MAX_QUESTIONS] if str(item).strip()]
    return ["**Open questions:**", *bullets, ""] if bullets else []


def _run_prompt_context(state: WorkflowState) -> PromptRunContext:
    return PromptRunContext(
        meta_review=state.get("meta_review"),
        tool_registry=state.get("tool_registry"),
        run_setup_guidance=state.get("run_setup_guidance"),
        run_focus_guidance=state.get("run_focus_guidance"),
    )


_UNREVIEWED_OVERVIEW: Final = {"reviewed": False, "rounds": 0}


async def _emit_and_synthesize_overview(
    state: WorkflowState,
    summary: str,
    contact_candidates: dict[str, dict[str, Any]],
    evidence_corpus: dict[str, dict[str, Any]],
    hypothesis_by_index: dict[int, str],
) -> dict[str, Any]:
    """Terminal progress percentages on an interim pass would drive progress
    to the end and backwards again."""
    if _is_interim_firing(state):
        return await _interim_overview_result(state, summary, evidence_corpus)

    await emit_progress(
        state,
        "research_overview_start",
        "Synthesizing research overview...",
        PROGRESS_RESEARCH_OVERVIEW_START,
    )

    research_overview, llm_calls = await _synthesize_research_overview(
        state, summary, contact_candidates, evidence_corpus, hypothesis_by_index
    )

    await emit_progress(
        state,
        "research_overview_complete",
        "Research overview ready",
        PROGRESS_RESEARCH_OVERVIEW_COMPLETE,
    )
    logger.info("Research overview complete")
    return _build_research_overview_result(research_overview, llm_calls)


async def research_overview_node(state: WorkflowState) -> dict[str, Any]:
    """Filter before synthesis: prose derived from withheld ideas cannot be
    unlabeled after the model has used them."""
    hypotheses = state.get("hypotheses", [])
    publishable = _publishable_hypotheses(hypotheses)
    if not publishable:
        if hypotheses:
            logger.warning(
                "Research overview skipped: publication gates withheld all "
                "%d hypotheses; nothing to synthesize",
                len(hypotheses),
            )
        return {"research_overview": {}}

    articles = state.get("articles")
    summary, hypothesis_by_index = _summarize_top_hypotheses(publishable)
    contact_candidates = _build_contact_candidates(articles)
    evidence_corpus = _build_evidence_corpus(articles)

    # Terminal task failure prevents finalization and report publication;
    # degrade the synthesis instead of losing completed scientific work.
    return await synthesize_or_degrade(
        state,
        lambda: _emit_and_synthesize_overview(
            state,
            summary,
            contact_candidates,
            evidence_corpus,
            hypothesis_by_index,
        ),
    )


async def _interim_overview_result(
    state: WorkflowState,
    summary: str,
    evidence_corpus: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The lean periodic call leaves terminal report/UI output untouched and
    buys neither accuracy review nor deep knowledge-base work."""
    # The interim schema asks for no contacts, so candidate context would buy no output.
    prompt, schema = get_research_overview_interim_prompt(
        research_goal=state["research_goal"],
        hypotheses_summary=summary,
        evidence_corpus=_format_evidence_corpus(evidence_corpus),
        context=_run_prompt_context(state),
    )
    response = await _call_research_overview_llm(
        state, prompt, schema, max_tokens=RESEARCH_OVERVIEW_INTERIM_MAX_TOKENS
    )
    logger.info("Interim research overview ready for the next cycle")
    return {
        "interim_overview": build_interim_overview(response),
        "metrics": create_metrics_update(deltas=MetricDeltas(llm_calls=1)),
        "messages": phase_message(
            "research_overview",
            "Synthesized an interim research overview",
        ),
    }


def _publishable_hypotheses(
    hypotheses: list[Hypothesis],
) -> list[Hypothesis]:
    return [h for h in hypotheses if h.is_rankable() and not is_blocking_status(h.safety_status)]


def _summarize_top_hypotheses(
    hypotheses: list[Hypothesis],
) -> tuple[str, dict[int, str]]:
    """Use publication ordering: undermined ideas may publish, but their old
    high Elo must not put them ahead of sound ideas in synthesis."""
    ranked = rank_for_publication(hypotheses)
    top = ranked[:RESEARCH_OVERVIEW_TOP_K]
    summary = "\n".join(f"{i + 1}. (Elo {h.elo_rating}) {h.text}" for i, h in enumerate(top))
    hypothesis_by_index = {i + 1: h.id for i, h in enumerate(top)}
    return summary, hypothesis_by_index


async def _synthesize_research_overview(
    state: WorkflowState,
    summary: str,
    contact_candidates: dict[str, dict[str, Any]],
    evidence_corpus: dict[str, dict[str, Any]],
    hypothesis_by_index: dict[int, str],
) -> tuple[dict[str, Any], int]:
    """Accuracy revision replaces the raw draft; validate grounding once,
    last, on the version that will actually publish."""
    prompt, schema = get_research_overview_prompt(
        research_goal=state["research_goal"],
        hypotheses_summary=summary,
        contact_candidates=_format_contact_candidates(contact_candidates),
        evidence_corpus=_format_evidence_corpus(evidence_corpus),
        context=_run_prompt_context(state),
    )
    response = await _call_research_overview_llm(state, prompt, schema)
    response, review_meta, review_calls = await _maybe_review_overview(
        state, summary, contact_candidates, evidence_corpus, response
    )
    formatted = _format_research_overview_response(
        response, contact_candidates, evidence_corpus, hypothesis_by_index
    )
    formatted["overview_review"] = review_meta
    wave = DirectionWaveContext(
        state=state,
        hypotheses_summary=summary,
        evidence_corpus_text=_format_evidence_corpus(evidence_corpus),
    )
    direction_calls = await develop_directions_into(wave, formatted, call_llm_json)
    deep_calls = await _deepen_knowledge_base(state, summary, evidence_corpus, formatted)
    return formatted, 1 + review_calls + direction_calls + deep_calls


async def _deepen_knowledge_base(
    state: WorkflowState,
    summary: str,
    evidence_corpus: dict[str, dict[str, Any]],
    formatted: dict[str, Any],
) -> int:
    """Unfunded or ungrounded deep output must leave the draft's flat topics
    intact."""
    if not knowledge_base_is_funded(state):
        return 0
    topics, calls = await synthesize_knowledge_base(
        state,
        summary,
        evidence_corpus,
        evidence_corpus_text=_format_evidence_corpus(evidence_corpus),
    )
    if topics:
        formatted["knowledge_base"] = topics
    return calls


async def _maybe_review_overview(
    state: WorkflowState,
    summary: str,
    contact_candidates: dict[str, dict[str, Any]],
    evidence_corpus: dict[str, dict[str, Any]],
    response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Failed optional review must not lose the report; publish the original
    draft with its review metadata rather than fail finalization."""
    if not state.get("enable_overview_review"):
        return response, dict(_UNREVIEWED_OVERVIEW), 0
    context = OverviewReviewContext(
        state=state,
        research_goal=state["research_goal"],
        hypotheses_summary=summary,
        contact_candidates_text=_format_contact_candidates(contact_candidates),
        evidence_corpus_text=_format_evidence_corpus(evidence_corpus),
    )
    try:
        return await review_research_overview(context, response)
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception:
        logger.error(
            "Research overview review failed; publishing the drafted overview unchanged",
            exc_info=True,
        )
        return response, dict(_UNREVIEWED_OVERVIEW), 0


async def _call_research_overview_llm(
    state: WorkflowState,
    prompt: str,
    schema: dict[str, Any] | None,
    max_tokens: int = RESEARCH_OVERVIEW_MAX_TOKENS,
) -> dict[str, Any]:
    """Thinking and the strategic document share the allowance; the interim
    schema needs its smaller budget rather than the full terminal ceiling."""
    return await call_llm_json(
        prompt=prompt,
        spec=CompletionSpec(
            role="overview",
            model_name=state["supervisor_model_name"],
            max_tokens=max_tokens,
            temperature=MEDIUM_TEMPERATURE,
            json_schema=schema,
        ),
    )


def _format_research_overview_response(
    response: dict[str, Any],
    contact_candidates: dict[str, dict[str, Any]],
    evidence_corpus: dict[str, dict[str, Any]],
    hypothesis_by_index: dict[int, str],
) -> dict[str, Any]:
    return {
        "overview": format_overview(response.get("overview", {})),
        "nih_specific_aims": response.get("nih_specific_aims", {}),
        "research_contacts": _validate_research_contacts(
            response.get("research_contacts"), contact_candidates
        ),
        "research_contact_groups": _validate_research_contact_groups(
            response.get("research_contact_groups"), hypothesis_by_index
        ),
        "knowledge_base": _validate_knowledge_base(response.get("knowledge_base"), evidence_corpus),
        "open_questions": response.get("open_questions", []),
        "clear_patterns": response.get("clear_patterns", []),
        "unexpected_patterns": response.get("unexpected_patterns", []),
        "unexpected_research_directions": response.get("unexpected_research_directions", []),
    }


def _build_research_overview_result(
    research_overview: dict[str, Any], llm_calls: int = 1
) -> dict[str, Any]:
    metrics = create_metrics_update(deltas=MetricDeltas(llm_calls=llm_calls))
    return {
        "research_overview": research_overview,
        "metrics": metrics,
        "messages": phase_message(
            "research_overview",
            "Synthesized research overview and Specific Aims",
        ),
    }


_is_interim_firing = is_interim_firing
