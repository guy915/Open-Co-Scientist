"""One huge answer cannot fit the provider clock; outline once, then write
bounded themes concurrently, falling back to grounded flat topics."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from co_scientist.core.constants import (
    KNOWLEDGE_BASE_OUTLINE_MAX_TOKENS,
    KNOWLEDGE_BASE_THEME_MAX_TOKENS,
    MEDIUM_TEMPERATURE,
    MINIMAL_REASONING_MAX_TOKENS,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
    scoped_telemetry_phase,
)
from co_scientist.science.meta_review.research_overview_evidence import (
    prompt_context,
)
from co_scientist.science.prompts import (
    ThemeWritingMaterial,
    get_knowledge_base_outline_prompt,
    get_knowledge_base_theme_prompt,
)
from co_scientist.science.schemas.synthesis import (
    KNOWLEDGE_BASE_MAX_SECTIONS,
    KNOWLEDGE_BASE_MAX_THEMES,
)

logger = logging.getLogger(__name__)


async def _ask(
    state: WorkflowState,
    prompt: str,
    schema: dict[str, Any] | None,
    max_tokens: int,
) -> dict[str, Any]:
    """Separate telemetry preserves draft/part spend attribution within the
    same node."""
    with scoped_telemetry_phase("knowledge_base"):
        return await call_llm_json(
            prompt=prompt,
            spec=CompletionSpec(
                role="overview_outline",
                model_name=state["supervisor_model_name"],
                max_tokens=max_tokens,
                temperature=MEDIUM_TEMPERATURE,
                json_schema=schema,
            ),
            options=LLMCallOptions(
                reasoning_max_tokens=MINIMAL_REASONING_MAX_TOKENS,
                prompt_name=str((schema or {}).get("name") or "knowledge_base"),
            ),
        )


def _outline_themes(raw_themes: Any) -> list[dict[str, Any]]:
    """json_object may ignore maxItems; extra or empty themes would buy
    unfunded writing requests, so enforce bounds before fan-out."""
    if not isinstance(raw_themes, list):
        return []
    themes: list[dict[str, Any]] = []
    for raw in raw_themes[:KNOWLEDGE_BASE_MAX_THEMES]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        raw_sections = raw.get("sections")
        sections = [
            section
            for section in (
                raw_sections[:KNOWLEDGE_BASE_MAX_SECTIONS] if isinstance(raw_sections, list) else []
            )
            if isinstance(section, dict)
        ]
        if not title or not sections:
            continue
        themes.append({"title": title, "sections": sections})
    return themes


async def plan_knowledge_base_outline(
    state: WorkflowState,
    hypotheses_summary: str,
    evidence_corpus_text: str,
) -> list[dict[str, Any]]:
    prompt, schema = get_knowledge_base_outline_prompt(
        research_goal=state["research_goal"],
        hypotheses_summary=hypotheses_summary,
        evidence_corpus=evidence_corpus_text,
        context=prompt_context(state),
    )
    try:
        response = await _ask(state, prompt, schema, KNOWLEDGE_BASE_OUTLINE_MAX_TOKENS)
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception:
        logger.error(
            "Knowledge-base outline failed; publishing the research overview's own topics instead",
            exc_info=True,
        )
        return []
    return _outline_themes(response.get("themes"))


def format_outline(themes: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for theme in themes:
        lines.append(f"## {theme['title']}")
        lines += [f"- {str(section.get('heading') or '').strip()}" for section in theme["sections"]]
    return "\n".join(lines)


def format_theme_sections(theme: dict[str, Any]) -> str:
    lines: list[str] = []
    for section in theme["sections"]:
        raw_ids = section.get("evidence_ids")
        ids = [
            item for item in (raw_ids if isinstance(raw_ids, list) else []) if isinstance(item, str)
        ]
        heading = str(section.get("heading") or "").strip()
        lines.append(f"- {heading} (evidence: {', '.join(ids) or 'none'})")
    return "\n".join(lines)


async def write_theme_sections(
    state: WorkflowState,
    theme: dict[str, Any],
    outline_text: str,
    evidence_corpus_text: str,
) -> list[dict[str, Any]]:
    """A failed theme must not abort its valid siblings in the concurrent
    wave."""
    prompt, schema = get_knowledge_base_theme_prompt(
        research_goal=state["research_goal"],
        material=ThemeWritingMaterial(
            title=theme["title"],
            sections=format_theme_sections(theme),
            outline=outline_text,
        ),
        evidence_corpus=evidence_corpus_text,
        context=prompt_context(state),
    )
    try:
        response = await _ask(state, prompt, schema, KNOWLEDGE_BASE_THEME_MAX_TOKENS)
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception:
        logger.error(
            "Knowledge-base theme %r failed; publishing the rest of the section without it",
            theme["title"],
            exc_info=True,
        )
        return []
    written = response.get("sections")
    if not isinstance(written, list):
        return []
    return [item for item in written if isinstance(item, dict)]


_MAX_KNOWLEDGE_BASE_TOPICS: Final = 8
# Keep the flat fallback shape where deep synthesis is unfunded or unavailable.

KNOWLEDGE_BASE_MIN_LLM_CALLS: Final = 2500
# Read the declared ceiling instead of duplicating tier policy; no declared
# budget does not fund deep synthesis.


def knowledge_base_is_funded(state: WorkflowState) -> bool:
    ceiling = (state.get("budget") or {}).get("max_llm_calls")
    return ceiling is not None and int(ceiling) >= KNOWLEDGE_BASE_MIN_LLM_CALLS


async def synthesize_knowledge_base(
    state: WorkflowState,
    hypotheses_summary: str,
    corpus: dict[str, dict[str, Any]],
    evidence_corpus_text: str = "",
) -> tuple[list[dict[str, Any]], int]:
    if not corpus:
        return [], 0
    themes = await plan_knowledge_base_outline(state, hypotheses_summary, evidence_corpus_text)
    if not themes:
        return [], 1
    outline_text = format_outline(themes)
    written = await asyncio.gather(
        *[
            write_theme_sections(state, theme, outline_text, evidence_corpus_text)
            for theme in themes
        ]
    )
    raw = [
        {"title": theme["title"], "sections": _grounded_sections(theme, part)}
        for theme, part in zip(themes, written, strict=True)
    ]
    return validate_themes(raw, corpus), 1 + len(themes)


def _grounded_sections(
    theme: dict[str, Any], written: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The outline owns grounding; restore omitted sources by exact heading
    so a writer's lost field does not imply a thin corpus."""
    outlined = {
        str(section.get("heading") or "").strip(): section.get("evidence_ids")
        for section in theme["sections"]
    }
    for section in written:
        if not section.get("evidence_ids"):
            heading = str(section.get("heading") or "").strip()
            section["evidence_ids"] = outlined.get(heading)
    return written


def _section_topic(
    raw: Any,
    theme: str,
    corpus: dict[str, dict[str, Any]],
    index: int,
) -> dict[str, Any] | None:
    """Deep output obeys the same analyzed-source gate as flat topics; depth
    cannot provide an alternate route for ungrounded claims."""
    if not isinstance(raw, dict):
        return None
    raw_ids = raw.get("evidence_ids")
    ids = [
        item
        for item in (raw_ids if isinstance(raw_ids, list) else [])
        if isinstance(item, str) and item in corpus
    ]
    heading = str(raw.get("heading") or "").strip()
    if not ids or not heading:
        return None
    return {
        "id": f"topic-{index}",
        "theme": theme,
        "title": heading,
        "summary": str(raw.get("summary") or "").strip(),
        "detail": str(raw.get("detail") or "").strip(),
        "uncertainty": "",
        "references": [corpus[item] for item in ids],
    }


def _theme_topics(
    raw_theme: Any, corpus: dict[str, dict[str, Any]], offset: int
) -> list[dict[str, Any]]:
    if not isinstance(raw_theme, dict):
        return []
    theme = str(raw_theme.get("title") or "").strip()
    raw_sections = raw_theme.get("sections")
    sections = raw_sections if isinstance(raw_sections, list) else []
    topics: list[dict[str, Any]] = []
    for raw in sections[:KNOWLEDGE_BASE_MAX_SECTIONS]:
        topic = _section_topic(raw, theme, corpus, offset + len(topics) + 1)
        if topic is not None:
            topics.append(topic)
    return topics


def validate_themes(raw_themes: Any, corpus: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """json_object does not enforce maxItems; reapply bounds before
    publication."""
    if not isinstance(raw_themes, list):
        return []
    topics: list[dict[str, Any]] = []
    for raw_theme in raw_themes[:KNOWLEDGE_BASE_MAX_THEMES]:
        topics += _theme_topics(raw_theme, corpus, len(topics))
    return topics


def _topic_evidence_ids(raw: Any, corpus: dict[str, dict[str, Any]]) -> list[str] | None:
    if not isinstance(raw, dict):
        return None
    raw_ids = raw.get("evidence_ids")
    if not isinstance(raw_ids, list):
        return None
    evidence_ids = [item for item in raw_ids if isinstance(item, str) and item in corpus]
    return evidence_ids or None


def _validate_knowledge_base(
    raw_topics: Any,
    corpus: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw_topics, list):
        return []
    topics: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_topics[:_MAX_KNOWLEDGE_BASE_TOPICS]):
        evidence_ids = _topic_evidence_ids(raw, corpus)
        if evidence_ids is None:
            continue
        topics.append(
            {
                "id": f"topic-{index + 1}",
                "title": str(raw.get("title") or "").strip(),
                "summary": str(raw.get("summary") or "").strip(),
                "detail": str(raw.get("detail") or "").strip(),
                "uncertainty": str(raw.get("uncertainty") or "").strip(),
                "references": [corpus[item] for item in evidence_ids],
            }
        )
    return topics
