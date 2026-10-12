"""arXiv's subject API covers fields Europe PMC's life-science index barely
reaches.
"""

import asyncio
import logging
import math
import re
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as ElementTree
import httpx

from mcp_server.http_client import make_client
from mcp_server.log_privacy import failure_summary
from mcp_server.tools._results import failed, non_raising, ok

logger = logging.getLogger(__name__)

_ARXIV_API_URL = "https://export.arxiv.org/api/query"
_ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.0
_ARXIV_MAX_RETRY_DELAY_SECONDS = 10.0
_ARXIV_REQUEST_LOCK = asyncio.Lock()
_ARXIV_NEXT_REQUEST_AT: float | None = None
_sleep = asyncio.sleep
_monotonic = time.monotonic

_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"

# Remove revision suffixes so newer versions keep the same paper identity.
_VERSION_SUFFIX_RE = re.compile(r"v\d+$")


def _atom(tag: str) -> str:
    return f"{{{_ATOM_NS}}}{tag}"


def _clean_text(text: str | None) -> str:
    return " ".join((text or "").split())


def _short_id(entry_id: str) -> str:
    """Old arXiv IDs include archive prefixes; splitting only the last slash
    loses identity.
    """
    tail = entry_id.rsplit("/abs/", 1)[-1]
    return _VERSION_SUFFIX_RE.sub("", tail)


def _entry_authors(entry: Element) -> list[str]:
    names = []
    for author in entry.findall(_atom("author")):
        name = author.findtext(_atom("name"))
        if name and name.strip():
            names.append(name.strip())
    return names


def _entry_year(entry: Element) -> int | None:
    published = entry.findtext(_atom("published")) or ""
    return int(published[:4]) if published[:4].isdigit() else None


def _entry_record(entry: Element) -> dict[str, Any]:
    """arXiv errors may be entries without IDs; do not surface them as
    synthetic papers.
    """
    raw_id = entry.findtext(_atom("id"))
    if not raw_id or "/api/errors" in raw_id:
        raise ValueError("arXiv returned a query error")
    return {
        "source_id": _short_id(raw_id),
        "title": _clean_text(entry.findtext(_atom("title"))),
        "abstract": _clean_text(entry.findtext(_atom("summary"))),
        "year": _entry_year(entry),
        "authors": _entry_authors(entry),
        "doi": entry.findtext(f"{{{_ARXIV_NS}}}doi"),
        # Journal publication does not turn the retrieved arXiv copy into peer-
        # reviewed evidence.
        "is_preprint": True,
        "url": raw_id,
    }


def _parse_feed(xml_text: str) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(xml_text)
    if root.tag != _atom("feed"):
        raise ValueError("arXiv returned an invalid feed")
    return [_entry_record(entry) for entry in root.findall(_atom("entry"))]


def _retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return _ARXIV_MIN_REQUEST_INTERVAL_SECONDS
    try:
        delay = float(value)
        return delay if math.isfinite(delay) and delay >= 0 else _ARXIV_MIN_REQUEST_INTERVAL_SECONDS
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return _ARXIV_MIN_REQUEST_INTERVAL_SECONDS


def _rate_limited_response() -> httpx.Response:
    request = httpx.Request("GET", _ARXIV_API_URL)
    return httpx.Response(429, request=request)


async def _paced_get(client: httpx.AsyncClient, params: dict[str, str]) -> httpx.Response:
    global _ARXIV_NEXT_REQUEST_AT

    async with _ARXIV_REQUEST_LOCK:
        for attempt in range(2):
            now = _monotonic()
            wait = 0.0 if _ARXIV_NEXT_REQUEST_AT is None else max(0.0, _ARXIV_NEXT_REQUEST_AT - now)
            if wait > _ARXIV_MAX_RETRY_DELAY_SECONDS:
                return _rate_limited_response()
            if wait > 0:
                await _sleep(wait)

            started_at = _monotonic()
            _ARXIV_NEXT_REQUEST_AT = started_at + _ARXIV_MIN_REQUEST_INTERVAL_SECONDS
            response = await client.get(_ARXIV_API_URL, params=params)
            if response.status_code != 429:
                return response

            _ARXIV_NEXT_REQUEST_AT = max(
                _ARXIV_NEXT_REQUEST_AT,
                _monotonic() + _retry_after_seconds(response),
            )
            if attempt == 1:
                return response
            if _ARXIV_NEXT_REQUEST_AT - _monotonic() > _ARXIV_MAX_RETRY_DELAY_SECONDS:
                return response
        raise AssertionError("bounded arXiv retry loop exhausted")


@non_raising
async def search_arxiv(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search arXiv for preprints matching a free-text query.

    Args:
        query: Free-text search query, matched across title, abstract,
            authors, and comments.
        max_results: Maximum records to return, capped at 25.

    Returns:
        Source-stamped preprint records, or an empty-records envelope on
        any failure to reach or parse arXiv's response.
    """
    limit = max(1, min(max_results, 25))
    params = {
        "search_query": f"all:{query}",
        "start": "0",
        "max_results": str(limit),
    }
    try:
        async with make_client(30) as client:
            async with asyncio.timeout(30):
                response = await _paced_get(client, params)
                response.raise_for_status()
        records = _parse_feed(response.text)[:limit]
    except (httpx.HTTPError, ParseError, TimeoutError) as exc:
        logger.warning("arXiv search failed (%s)", failure_summary(exc))
        return failed(exc)
    return ok(records)
