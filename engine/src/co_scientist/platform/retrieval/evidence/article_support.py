from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from co_scientist.core.constants import (
    LITERATURE_REVIEW_FAILED,
    PROMPT_PAPER_MAX_CHARS,
    strip_citation_markers,
    truncate_for_prompt,
)
from co_scientist.core.metrics import phase_message
from co_scientist.platform.retrieval.article import Article
from co_scientist.platform.retrieval.research import Finding, ResearchResult

if TYPE_CHECKING:
    from co_scientist.platform.retrieval.research_adapter import McpRetrieval


def records_from_findings(
    result: ResearchResult, retrieval: McpRetrieval
) -> dict[str, dict[str, Any]]:
    """A search hit alone must not enter downstream prompts; retain only
    analyzed findings with their query/source provenance."""
    records: dict[str, dict[str, Any]] = {}
    for finding in result.findings:
        if finding.locator in records:
            continue
        record = retrieval.record(finding.locator)
        if record is None:
            continue
        record["retrieval_call_id"] = finding.call_id
        record.setdefault("_source_name", _source_of(result, finding))
        records[finding.locator] = record
    return records


def _source_of(result: ResearchResult, finding: Finding) -> str:
    for call in result.calls:
        if call.id == finding.call_id:
            return call.source
    return ""


logger = logging.getLogger(__name__)


# One PubMed parser writes this where a value is absent, and cached metadata
# files still hold it. Left as a string it is a truthy DOI that every
# unidentified paper shares: it resolves as a URL and merges distinct papers.
_MISSING_VALUE_SENTINEL = "<not found>"


def present_value(value: Any) -> Any:
    return None if value == _MISSING_VALUE_SENTINEL else value


def _metadata_is_retracted(metadata: dict[str, Any]) -> bool:
    publication_types = metadata.get("publication_types") or []
    if isinstance(publication_types, str):
        publication_types = [publication_types]
    normalized_types = {str(item).lower() for item in publication_types}
    return bool(
        metadata.get("is_retracted")
        or str(metadata.get("correction_status") or "").lower() == "retracted"
        or "retracted publication" in normalized_types
    )


def _publication_type(metadata: dict[str, Any]) -> str | None:
    """PubMed mixes preprints and journal articles in publication_types; joining
    all types preserves the signal consumed by the citation gate."""
    declared = metadata.get("publication_type")
    if declared:
        return str(declared)
    types = metadata.get("publication_types") or []
    if isinstance(types, str):
        types = [types]

    return "; ".join(str(item) for item in types) or None


def build_article_from_metadata(
    paper_id: str,
    metadata: dict[str, Any],
    source_name: str = "pubmed",
    used_in_analysis: bool = True,
) -> Article:
    year = parse_year_from_metadata(metadata)
    url = _build_article_url(paper_id, metadata, source_name)
    is_retracted = _metadata_is_retracted(metadata)

    return Article(
        title=metadata.get("title", "unknown"),
        url=url,
        authors=metadata.get("authors", []),
        year=year,
        venue=metadata.get("publication") or metadata.get("venue"),
        citations=0,
        abstract=present_value(metadata.get("abstract")),
        content=metadata.get("fulltext"),
        source_id=paper_id,
        source=source_name,
        doi=present_value(metadata.get("doi")),
        is_retracted=is_retracted,
        correction_status=str(
            metadata.get("correction_status") or ("retracted" if is_retracted else "current")
        ),
        publication_type=_publication_type(metadata),
        pdf_links=[],
        used_in_analysis=used_in_analysis,
        # Collection time differs from eventual persistence time.
        retrieved_at=time.time(),
        retrieval_score=metadata.get("retrieval_score"),
        retrieval_rationale=metadata.get("retrieval_rationale"),
        retriever_version=metadata.get("retriever_version"),
        retrieval_call_id=metadata.get("retrieval_call_id"),
    )


def _year_from_year_field(metadata: dict[str, Any]) -> int | None:
    if not (metadata.get("year")):
        return None
    try:
        return int(metadata["year"])
    except (ValueError, TypeError):
        return None


def _year_from_date_revised(metadata: dict[str, Any]) -> int | None:
    """Some sources expose only a revision date rather than a publication
    year."""
    if "date_revised" not in metadata:
        return None
    try:
        return int(metadata["date_revised"].split("/")[0])
    except (ValueError, KeyError, IndexError, AttributeError):
        return None


_YEAR_PARSERS: tuple[Callable[[dict[str, Any]], int | None], ...] = (
    _year_from_year_field,
    _year_from_date_revised,
)


def parse_year_from_metadata(metadata: dict[str, Any]) -> int | None:
    for parser in _YEAR_PARSERS:
        year = parser(metadata)
        if year is not None:
            return year
    return None


def _build_article_url(paper_id: str, metadata: dict[str, Any], source_name: str) -> str:

    url = metadata.get("url")
    if url:
        return cast(str, url)

    if source_name == "pubmed":
        return f"https://pubmed.ncbi.nlm.nih.gov/{paper_id}/"

    if paper_id.startswith("10."):
        return f"https://doi.org/{paper_id}"

    return paper_id


def build_articles_from_metadata(
    all_paper_metadata: dict[str, dict[str, Any]],
    default_source_name: str,
) -> list[Article]:
    articles = []
    for paper_id, metadata in all_paper_metadata.items():
        if isinstance(metadata, dict):
            # Originating source selects later PDF/content retrieval tools.
            paper_source = metadata.get("_source_name", default_source_name)
        else:
            paper_source = default_source_name

        articles.append(
            build_article_from_metadata(
                paper_id,
                metadata,
                paper_source,
                # An abstract supports shallower analysis; it does not imply fulltext retrieval.
                used_in_analysis=bool(metadata.get("fulltext") or metadata.get("abstract")),
            )
        )
    return articles


def _has_fulltext(meta: dict[str, Any]) -> bool:
    """MCP sources populate different fulltext indicators, so availability is
    a union."""
    return bool(
        meta.get("pmc_full_text_id")
        or meta.get("fulltext")
        or meta.get("has_fulltext")
        or meta.get("pdf_url")
    )


def count_papers_with_fulltext(
    all_paper_metadata: dict[str, dict[str, Any]],
) -> tuple[int, int]:
    with_fulltext = 0
    for meta in all_paper_metadata.values():
        if isinstance(meta, dict) and _has_fulltext(meta):
            with_fulltext += 1

    without_fulltext = len(all_paper_metadata) - with_fulltext
    return with_fulltext, without_fulltext


def get_papers_with_content(
    all_paper_metadata: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    papers_with_content = {}
    for pid, metadata in all_paper_metadata.items():
        if not isinstance(metadata, dict) or not (
            metadata.get("fulltext") or metadata.get("abstract")
        ):
            continue
        papers_with_content[pid] = metadata
        if not metadata.get("fulltext"):
            logger.debug(
                "Paper %s: using abstract for analysis (fulltext not downloaded)",
                pid,
            )
    return papers_with_content


def get_paper_content_for_analysis(
    metadata: dict[str, Any], max_chars: int = PROMPT_PAPER_MAX_CHARS
) -> str:
    """Strip markers only from the prompt copy, never stored articles.
    Truncate first to bound regex work; cut markers fail harmlessly."""

    content = str(metadata.get("fulltext") or metadata.get("abstract") or "")
    if len(content) > max_chars:
        logger.debug("Truncating paper content to %s chars", max_chars)
    return strip_citation_markers(truncate_for_prompt(content, max_chars))


def make_failure_result(
    reason: str,
    queries: list[str] | None = None,
    articles: list[Article] | None = None,
) -> dict[str, Any]:
    """Downstream generation checks this sentinel, not the presence of search
    data."""

    return {
        "articles_with_reasoning": LITERATURE_REVIEW_FAILED,
        "literature_review_queries": queries or [],
        "articles": articles or [],
        "messages": phase_message(
            "literature_review",
            f"literature review failed - {reason}",
            error=True,
        ),
    }


def make_success_result(
    synthesis: str,
    queries: list[str],
    articles: list[Article],
) -> dict[str, Any]:
    analyzed = [article for article in articles if article.used_in_analysis]
    return {
        "articles_with_reasoning": synthesis,
        "literature_review_queries": queries,
        "articles": articles,
        "messages": phase_message(
            "literature_review",
            f"completed literature review with {len(queries)}"
            f" queries, {len(analyzed)} of {len(articles)} articles analyzed",
            articles_retrieved=len(articles),
            articles_analyzed=len(analyzed),
            fulltext_analyzed=sum(bool(article.content) for article in analyzed),
            abstract_only_analyzed=sum(
                bool(not article.content and article.abstract) for article in analyzed
            ),
        ),
    }
