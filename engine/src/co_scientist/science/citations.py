from dataclasses import dataclass, field
from typing import Any

from co_scientist.core.citations import citation_keys_in as citation_keys_in
from co_scientist.core.constants import INITIAL_ELO_RATING, truncate
from co_scientist.domains.research_state.models import GenerationMethod, Hypothesis
from co_scientist.science.schemas.generation import (
    _EXPERIMENT_CRITERION_CHARS,
    _EXPERIMENT_STEP_CHARS,
    MAX_EXPERIMENT_STEPS,
)


def _experiment_steps(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    steps: list[str] = []
    for step in raw:
        text = str(step).strip() if step is not None else ""
        if text:
            steps.append(truncate(text, _EXPERIMENT_STEP_CHARS))
        if len(steps) == MAX_EXPERIMENT_STEPS:
            break
    return steps


def format_experiment_plan(data: Any, fallback: str | None = None) -> str | None:
    if isinstance(data, str):
        text = data.strip()
        return text or fallback
    if not isinstance(data, dict):
        return fallback
    steps = _experiment_steps(data.get("steps"))
    go = truncate(
        str(data.get("go_criterion") or "").strip(),
        _EXPERIMENT_CRITERION_CHARS,
    )
    no_go = truncate(
        str(data.get("no_go_criterion") or "").strip(),
        _EXPERIMENT_CRITERION_CHARS,
    )
    lines = [f"{i}. {step}" for i, step in enumerate(steps, start=1)]
    if go:
        lines.append(f"**Go:** {go}")
    if no_go:
        lines.append(f"**No-Go:** {no_go}")
    return "\n".join(lines) if lines else fallback


@dataclass
class ReferenceIndex:
    text: str

    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.sources


def _paper_citation_label(authors: list[str], year: int | None, title: str) -> str:
    # Author strings use First [Middle] Last format; the final token supplies
    # the surname.

    first_author = authors[0].strip().split()[-1] if authors else "Unknown"

    return f"{first_author} et al., {year}" if year else title[:50]


def _article_paper_fields(
    article: Any,
) -> tuple[str, str, list[str], int | None]:
    title = getattr(article, "title", "") or ""
    url = getattr(article, "url", "") or ""
    authors = getattr(article, "authors", []) or []
    year = getattr(article, "year", None)
    return title, url, authors, year


def _paper_reference_entries(
    articles: list[Any] | None,
    start_counter: int,
) -> tuple[list[str], dict[str, dict[str, Any]], int]:
    """Only papers actually analyzed can ground hypotheses; search hits alone
    are not evidence."""
    sources: dict[str, dict[str, Any]] = {}
    lines: list[str] = []
    counter = start_counter

    for article in articles or []:
        if not getattr(article, "used_in_analysis", False):
            continue
        key = f"C{counter}"
        title, url, authors, year = _article_paper_fields(article)
        label = _paper_citation_label(authors, year, title)
        lines.append(f"[{key}] {label} — {title[:80]}")
        sources[key] = {
            "type": "paper",
            "title": title,
            "url": url,
            "authors": authors,
            "year": year,
            "is_retracted": bool(getattr(article, "is_retracted", False)),
            "correction_status": getattr(article, "correction_status", "current"),
        }
        counter += 1

    return lines, sources, counter


def _enrichment_reference_entries(
    context_enrichment_sources: list[dict[str, Any]] | None,
    start_counter: int,
) -> tuple[list[str], dict[str, dict[str, Any]], int]:
    sources: dict[str, dict[str, Any]] = {}
    lines: list[str] = []
    counter = start_counter

    for item in context_enrichment_sources or []:
        key = f"C{counter}"
        display = item.get("display", "External source")
        lines.append(f"[{key}] {display}")
        sources[key] = {
            "type": item.get("source_type", "knowledge_graph"),
            "display": display,
            "tool_id": item.get("tool_id", ""),
            "data": item.get("data", {}),
        }
        counter += 1

    return lines, sources, counter


def build_reference_index(
    articles: list[Any] | None,
    context_enrichment_sources: list[dict[str, Any]] | None,
) -> ReferenceIndex:
    """Papers and enrichment share one continuous [C*] namespace used by
    every generation strategy."""

    paper_lines, sources, counter = _paper_reference_entries(articles, 1)
    enrichment_lines, enrichment_sources, _ = _enrichment_reference_entries(
        context_enrichment_sources, counter
    )
    sources.update(enrichment_sources)

    return ReferenceIndex(text="\n".join(paper_lines + enrichment_lines), sources=sources)


def _record_citation_key(
    key: str,
    sources: dict[str, dict[str, Any]],
    seen: set[str],
    result: dict[str, dict[str, Any]],
) -> None:
    """Drop hallucinated citation keys: citation_map is best-effort metadata,
    not the evidence correctness gate."""
    if key not in sources or key in seen:
        return
    result[key] = sources[key]
    seen.add(key)


def resolve_citation_keys(
    literature_grounding: str | None,
    sources: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:

    if not literature_grounding or not sources:
        return {}

    seen: set[str] = set()
    result: dict[str, dict[str, Any]] = {}
    for key in citation_keys_in(literature_grounding):
        _record_citation_key(key, sources, seen, result)
    return result


def hypothesis_from_llm_output(
    hyp_data: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    generation_method: GenerationMethod,
    **extra: Any,
) -> Hypothesis:
    """Title fallback belongs to the app drain; preserve raw values here
    instead of deriving a second title."""

    literature_grounding = hyp_data.get("literature_grounding")
    return Hypothesis(
        # Generation schemas disagree on hypothesis/text; accept both raw
        # shapes.
        text=hyp_data.get("hypothesis") or hyp_data.get("text", ""),
        title=hyp_data.get("title"),
        category=hyp_data.get("category"),
        introduction=hyp_data.get("introduction"),
        recent_findings=hyp_data.get("recent_findings"),
        safety_and_toxicity=hyp_data.get("safety_and_toxicity"),
        explanation=hyp_data.get("explanation"),
        literature_grounding=literature_grounding,
        experiment=format_experiment_plan(hyp_data.get("experiment")),
        score=0.0,
        elo_rating=INITIAL_ELO_RATING,
        generation_method=generation_method,
        citation_map=resolve_citation_keys(literature_grounding, sources),
        **extra,
    )
