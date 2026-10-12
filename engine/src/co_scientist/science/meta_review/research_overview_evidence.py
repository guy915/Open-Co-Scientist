from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any, Final

from co_scientist.core.constants import strip_citation_markers
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.article import Article
from co_scientist.science.prompts import PromptRunContext

_MAX_CONTACT_CANDIDATES: Final = 30

_MAX_RESEARCH_CONTACTS: Final = 5

_MAX_RESEARCH_CONTACT_GROUPS: Final = 5
# More groups than contacts cannot add meaningful associations.

_MAX_GROUP_EXAMPLE_HYPOTHESES: Final = 2
# The published exemplar offers two Example Hypothesis Titles per direction.


def _build_contact_candidates(
    articles: list[Article] | None,
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    seen_names: set[str] = set()
    for article_index, article in enumerate(articles or []):
        if not article.used_in_analysis:
            continue
        for author_index, raw_name in enumerate(article.authors or []):
            name = raw_name.strip()
            normalized = name.casefold()
            if not name or normalized in seen_names or len(candidates) >= _MAX_CONTACT_CANDIDATES:
                continue
            candidate_id = f"author-{article_index + 1}-{author_index + 1}"
            candidates[candidate_id] = {
                "candidate_id": candidate_id,
                "name": name,
                "source_id": article.source_id or "",
                "source_title": article.title,
                "source_url": article.url or "",
                "source": article.source,
            }
            seen_names.add(normalized)
    return candidates


def _format_or_placeholder(
    records: dict[str, dict[str, Any]], template: str, empty_message: str
) -> str:
    if not records:
        return empty_message
    return "\n".join(template.format(**record) for record in records.values())


def _format_contact_candidates(
    candidates: dict[str, dict[str, Any]],
) -> str:
    return _format_or_placeholder(
        candidates,
        ("- {candidate_id}: {name}; paper={source_title}; source_id={source_id}; url={source_url}"),
        "No verified literature authors available.",
    )


def _valid_candidate_id(raw: Any, seen: set[str]) -> str | None:
    if not isinstance(raw, dict):
        return None
    candidate_id = raw.get("candidate_id")
    if not isinstance(candidate_id, str) or candidate_id in seen:
        return None
    return candidate_id


def _matching_candidate(
    candidate_id: str,
    raw: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    candidate = candidates.get(candidate_id)
    if candidate is None:
        return None
    if raw.get("name") != candidate["name"]:
        return None
    return candidate


def _iter_matching_contacts(
    raw_contacts: list[Any],
    candidates: dict[str, dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    seen: set[str] = set()
    for raw in raw_contacts:
        candidate_id = _valid_candidate_id(raw, seen)
        if candidate_id is None:
            continue
        candidate = _matching_candidate(candidate_id, raw, candidates)
        if candidate is None:
            continue
        seen.add(candidate_id)
        yield raw, candidate


def _validate_research_contacts(
    raw_contacts: Any,
    candidates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw_contacts, list):
        return []
    matches = itertools.islice(
        _iter_matching_contacts(raw_contacts, candidates),
        _MAX_RESEARCH_CONTACTS,
    )
    return [
        {
            **candidate,
            "expertise": str(raw.get("expertise") or "").strip(),
            "justification": str(raw.get("justification") or "").strip(),
            "research_direction": str(raw.get("research_direction") or "").strip(),
        }
        for raw, candidate in matches
    ]


def _resolve_example_hypothesis_ids(
    raw_indices: Any, hypothesis_by_index: dict[int, str]
) -> list[str]:
    """One-based indices avoid echoed text that grows with the pool and
    truncates the same way on every retry."""
    if not isinstance(raw_indices, list):
        return []
    ids: list[str] = []
    for raw in itertools.islice(raw_indices, _MAX_GROUP_EXAMPLE_HYPOTHESES):
        hyp_id = hypothesis_by_index.get(raw) if isinstance(raw, int) else None
        if hyp_id is not None and hyp_id not in ids:
            ids.append(hyp_id)
    return ids


def _validate_research_contact_group(
    raw: Any, hypothesis_by_index: dict[int, str]
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    direction = str(raw.get("research_direction") or "").strip()
    if not direction:
        return None
    return {
        "research_direction": direction,
        "rationale": str(raw.get("rationale") or "").strip(),
        "example_hypothesis_ids": _resolve_example_hypothesis_ids(
            raw.get("example_hypothesis_indices"), hypothesis_by_index
        ),
    }


def _validate_research_contact_groups(
    raw_groups: Any, hypothesis_by_index: dict[int, str]
) -> list[dict[str, Any]]:
    """Directions are free labels, unlike verified names; unmatched labels
    render no contacts rather than inventing associations."""
    if not isinstance(raw_groups, list):
        return []
    validated = (_validate_research_contact_group(raw, hypothesis_by_index) for raw in raw_groups)
    return list(
        itertools.islice(
            (group for group in validated if group is not None),
            _MAX_RESEARCH_CONTACT_GROUPS,
        )
    )


_EVIDENCE_ABSTRACT_CHARS: Final = 3000
# Per-source and source-count caps bound independent growth; whole fulltext
# would exceed context. Detail needs selected passages, not a raised cap.


RESEARCH_OVERVIEW_MAX_SOURCES: Final = 130
# 130 sources measured about 52k corpus tokens and fit the provider context; the
# same cap protects all synthesis callers.


def _interleave_by_source(articles: list[Article]) -> list[Article]:
    """Source-clustered retrieval biases citations toward the head;
    interleave to expose breadth while preserving each source's order."""
    groups: dict[str, list[Article]] = {}
    for article in articles:
        groups.setdefault(article.source, []).append(article)
    interleaved: list[Article] = []
    for row in itertools.zip_longest(*groups.values()):
        interleaved.extend(article for article in row if article is not None)
    return interleaved


def _build_evidence_corpus(
    articles: list[Article] | None,
) -> dict[str, dict[str, Any]]:
    """Do not globally score-sort: web scores floor at zero and would
    disappear. Contiguous evidence IDs are opaque handles, not a ranking."""
    analyzed = [article for article in (articles or []) if article.used_in_analysis]
    selected = _interleave_by_source(analyzed)[:RESEARCH_OVERVIEW_MAX_SOURCES]
    corpus: dict[str, dict[str, Any]] = {}
    for index, article in enumerate(selected):
        evidence_id = f"evidence-{index + 1}"
        corpus[evidence_id] = {
            "evidence_id": evidence_id,
            "source_id": article.source_id or "",
            "title": article.title,
            "abstract": (article.abstract or "")[:_EVIDENCE_ABSTRACT_CHARS],
            "source": article.source,
            "url": article.url or "",
        }
    return corpus


def _format_evidence_corpus(corpus: dict[str, dict[str, Any]]) -> str:
    """Strip citation contamination from the prompt copy only; persisted
    source metadata must retain the actual retrieved excerpt."""
    for_prompt = {
        evidence_id: {
            **record,
            "abstract": strip_citation_markers(record["abstract"]),
        }
        for evidence_id, record in corpus.items()
    }
    return _format_or_placeholder(
        for_prompt,
        (
            "- {evidence_id}: title={title}; source={source}; "
            "source_id={source_id}; abstract={abstract}"
        ),
        "No verified evidence corpus available.",
    )


def prompt_context(state: WorkflowState) -> PromptRunContext:
    return PromptRunContext(
        tool_registry=state.get("tool_registry"),
        run_setup_guidance=state.get("run_setup_guidance"),
        run_focus_guidance=state.get("run_focus_guidance"),
    )
