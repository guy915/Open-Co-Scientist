"""Failed retrieval is distinct from no matches, enabling upstream retry and
degradation reporting.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from mcp_server.http_client import make_client
from mcp_server.log_privacy import failure_summary
from mcp_server.tools._results import failed, keyed_records, non_raising

logger = logging.getLogger(__name__)

_OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_MAX_PER_PAGE = 100  # Current documented OpenAlex page-size ceiling.

# OpenAlex stemmed search rejects wildcards; Boolean structure and quoted
# phrases pass through.
_WILDCARD_CHARS_RE = re.compile(r"[*?]")
_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_query(query: str) -> str:
    """OpenAlex stemmed search rejects wildcard characters with HTTP 400."""
    stripped = _WILDCARD_CHARS_RE.sub("", query)
    cleaned = _WHITESPACE_RE.sub(" ", stripped).strip()
    if cleaned != query:
        logger.info("Normalized OpenAlex query syntax")
    return cleaned


def _reconstruct_abstract(inverted_index: Any) -> str:
    if not isinstance(inverted_index, dict):
        return ""
    positions = [
        (position, str(word))
        for word, offsets in inverted_index.items()
        if isinstance(offsets, list)
        for position in offsets
        if isinstance(position, int)
    ]
    positions.sort(key=lambda item: item[0])
    return " ".join(word for _, word in positions)


def normalize_works(data: dict[str, Any], max_papers: int) -> dict[str, Any]:
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ValueError("invalid OpenAlex results")
    normalized: dict[str, Any] = {}
    for work in results[: max(max_papers, 0)]:
        if not isinstance(work, dict):
            continue
        raw_id = str(work.get("id") or "")
        work_id = raw_id.rsplit("/", 1)[-1]
        if not work_id:
            continue
        authors = []
        for authorship in work.get("authorships") or []:
            if isinstance(authorship, dict):
                name = (authorship.get("author") or {}).get("display_name", "")
                if isinstance(name, str) and name:
                    authors.append(name)
        location = work.get("primary_location") or {}
        landing_page = location.get("landing_page_url") if isinstance(location, dict) else None
        normalized[work_id] = {
            "title": work.get("title") or work.get("display_name") or "",
            "authors": authors,
            "year": work.get("publication_year"),
            "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
            "url": landing_page or work.get("doi") or raw_id,
            "source": "openalex",
            "cited_by_count": work.get("cited_by_count", 0),
            "publication_date": work.get("publication_date"),
            "updated_date": work.get("updated_date"),
            "work_type": work.get("type"),
            "is_retracted": bool(work.get("is_retracted", False)),
        }
    return normalized


def _build_search_params(
    query: str, max_papers: int, recency_years: int
) -> tuple[dict[str, str], int]:
    per_page = min(max(max_papers, 1), _MAX_PER_PAGE)
    params: dict[str, str] = {
        "search": _sanitize_query(query),
        "per_page": str(per_page),
        "cursor": "*",
    }
    # NCBI contact email also qualifies OpenAlex requests for its faster polite
    # pool.
    mailto = os.environ.get("ENTREZ_EMAIL") or os.environ.get("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto
    api_key = os.environ.get("OPENALEX_API_KEY")
    if api_key:
        params["api_key"] = api_key
    filters = ["is_retracted:false"]
    if recency_years and recency_years > 0:
        from_year = datetime.now(timezone.utc).year - recency_years
        filters.append(f"from_publication_date:{from_year}-01-01")
    params["filter"] = ",".join(filters)
    return params, per_page


def _next_cursor(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return None
    cursor = meta.get("next_cursor")
    return str(cursor) if cursor else None


async def _collect_openalex_works(
    params: dict[str, str], per_page: int, max_papers: int
) -> dict[str, Any]:
    collected: dict[str, Any] = {}
    async with make_client(30) as client:
        while len(collected) < max(max_papers, 0):
            params["per_page"] = str(min(per_page, max_papers - len(collected)))
            resp = await client.get(_OPENALEX_WORKS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            page = normalize_works(data, max_papers - len(collected))
            collected.update(page)
            cursor = _next_cursor(data)
            if not page or not cursor:
                break
            params["cursor"] = cursor
    return collected


@non_raising
async def search_openalex(
    query: str,
    max_papers: int = 10,
    recency_years: int = 0,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Search OpenAlex works and return ``{work_id: metadata}``.

    Args:
        query: Free-text search query.
        max_papers: Maximum number of works to return across cursor pages.
        recency_years: If > 0, restrict to works published within this many
            years.
        run_id: Unused; accepted for interface parity with other search tools.

    Returns:
        A dict of normalized works, empty when OpenAlex answered and had
        no match. Retrieval failures return the standard failure envelope.
    """
    params, per_page = _build_search_params(query, max_papers, recency_years)
    try:
        return keyed_records(await _collect_openalex_works(params, per_page, max_papers))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OpenAlex search failed (%s)", failure_summary(exc))
        return failed(exc)
