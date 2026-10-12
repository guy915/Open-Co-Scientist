import hashlib
import html
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from mcp_server.http_client import make_client
from mcp_server.log_privacy import failure_summary
from mcp_server.tools._results import failed, keyed_records, non_raising

logger = logging.getLogger(__name__)

# Clean snippets like fetched pages so source markup never reaches the agent.
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def clean_snippet(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    # Unescape after stripping tags so an encoded "&lt;b&gt;" in the source
    # text cannot reintroduce a tag that the strip already removed.
    text = html.unescape(_TAG_RE.sub("", raw))
    return _WHITESPACE_RE.sub(" ", text).strip()


# 401/402/403 and Tavily 432/433 require changed credentials/quota. Brave
# answers an exhausted month or credit with 429 and one of these codes; its
# per-second RATE_LIMITED 429 self-heals and moves only the current query.
_KEY_REJECTED_STATUSES = frozenset({401, 402, 403, 432, 433})
_QUOTA_EXHAUSTED_CODES = frozenset({"QUOTA_LIMITED", "USAGE_LIMIT_EXCEEDED", "CREDIT_EXHAUSTED"})
_RATE_LIMITED = "HTTP 429"

# Brave's X-RateLimit-Reset lists seconds from now per window ("1, 1419704":
# per-second, then per-month). Without a readable value, ask again a day later;
# the cap stops a malformed value from parking the provider beyond one month.
_QUOTA_RETRY_FALLBACK = 24 * 60 * 60
_QUOTA_RETRY_CEILING = 31 * 24 * 60 * 60

# Refusals describe process credentials, not one query, and govern subsequent
# provider choices.
_credential_errors: dict[str, dict[str, Any]] = {}
# Monotonic time after which a quota refusal lets the next search ask again.
_quota_retry_at: dict[str, float] = {}
_clock: Callable[[], float] = time.monotonic


def web_search_credential_error() -> dict[str, Any] | None:
    if not _credential_errors:
        return None
    return next(reversed(_credential_errors.values()))


def credential_error_for(provider: str) -> dict[str, Any] | None:
    return _credential_errors.get(provider)


def _record_credential_error(provider: str, status: int, detail: str) -> None:
    """Reinsert refusals so the status route reports the latest provider
    failure.
    """
    _credential_errors.pop(provider, None)
    _credential_errors[provider] = {
        "provider": provider,
        "status": status,
        # This record is returned by unauthenticated health checks. Provider
        # exceptions include private search URLs, even with ordinary HTTPX errors.
        "detail": f"HTTP {status}",
    }


def _clear_credential_error(provider: str | None = None) -> None:
    if provider is None:
        _credential_errors.clear()
        _quota_retry_at.clear()
    else:
        _credential_errors.pop(provider, None)
        _quota_retry_at.pop(provider, None)


def _refusal_lifted(provider: str) -> bool:
    if credential_error_for(provider) is None:
        return True
    retry_at = _quota_retry_at.get(provider)
    return retry_at is not None and _clock() >= retry_at


def _quota_reset_seconds(response: httpx.Response) -> float:
    try:
        windows = [int(part) for part in response.headers["x-ratelimit-reset"].split(",")]
    except (KeyError, ValueError):
        return _QUOTA_RETRY_FALLBACK
    # A refusal whose windows all reset now says nothing about when it ends.
    if min(windows) < 0 or max(windows) == 0:
        return _QUOTA_RETRY_FALLBACK
    return min(max(windows), _QUOTA_RETRY_CEILING)


def _error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _refused_status(exc: Exception) -> int | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    status = exc.response.status_code
    code = _error_code(exc.response)
    refused = status in _KEY_REJECTED_STATUSES or (
        code is not None and code in _QUOTA_EXHAUSTED_CODES
    )
    return status if refused else None


def _handle_provider_error(provider: str, query: str, exc: Exception) -> dict[str, Any]:
    status = _refused_status(exc)
    if status is not None:
        _record_credential_error(provider, status, str(exc))
        if (
            isinstance(exc, httpx.HTTPStatusError)
            and status not in _KEY_REJECTED_STATUSES
            and _error_code(exc.response) in _QUOTA_EXHAUSTED_CODES
        ):
            delay = _quota_reset_seconds(exc.response)
            _quota_retry_at[provider] = _clock() + delay
            logger.error(
                "%s quota is exhausted (HTTP %s) - searches move to the next "
                "provider, if one is configured, and ask it again in %d s",
                provider,
                status,
                delay,
            )
            return failed(exc)
        _quota_retry_at.pop(provider, None)
        # Logged at error, not warning: this one does not clear on its own,
        # and the search moves to another provider or returns nothing.
        logger.error(
            "%s refused the configured API key (HTTP %s) - searches move to "
            "the next provider, if one is configured",
            provider,
            status,
        )
        return failed(exc)
    logger.warning("%s web search failed (%s)", provider, failure_summary(exc))
    return failed(exc)


_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_TAVILY_URL = "https://api.tavily.com/search"

_REQUEST_TIMEOUT = 30

# Brave expresses recency as a coarse bucket rather than a day count.
_BRAVE_FRESHNESS_BUCKETS = ((1, "pd"), (7, "pw"), (31, "pm"), (365, "py"))


def _result_id(prefix: str, index: int, url: str) -> str:
    """Use a stable URL digest: Python hash salt would change source identity
    across processes.
    """
    digest = hashlib.blake2b(url.encode(), digest_size=4).hexdigest()
    return f"{prefix}-{index}-{digest}"


def _brave_freshness(recency_days: int) -> str | None:
    if recency_days <= 0:
        return None
    for threshold, code in _BRAVE_FRESHNESS_BUCKETS:
        if recency_days <= threshold:
            return code
    return None


def _result_metadata(
    item: dict[str, Any],
    url: str,
    provider_fields: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    return {
        "title": clean_snippet(item.get("title")),
        "url": url,
        "source": "web",
        **provider_fields(item),
    }


def _normalize_results(
    results: Any,
    max_results: int,
    prefix: str,
    provider_fields: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(results, list):
        raise ValueError("invalid web results")

    out: dict[str, Any] = {}
    for index, item in enumerate(results[: max(max_results, 0)]):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url:
            continue
        key = _result_id(prefix, index, url)
        out[key] = _result_metadata(item, url, provider_fields)
    return out


def normalize_brave(data: Any, max_results: int) -> dict[str, Any]:

    def fields(item: dict[str, Any]) -> dict[str, Any]:
        # Brave descriptions feed the article abstract field consumed
        # downstream.
        return {
            "abstract": clean_snippet(item.get("description")),
            "published_date": item.get("page_age") or item.get("age") or "",
            "site": (item.get("profile") or {}).get("name")
            if isinstance(item.get("profile"), dict)
            else "",
        }

    web = data.get("web") if isinstance(data, dict) else None
    results = web.get("results") if isinstance(web, dict) else None
    return _normalize_results(results, max_results, "brave", fields)


def normalize_tavily(data: Any, max_results: int) -> dict[str, Any]:

    def fields(item: dict[str, Any]) -> dict[str, Any]:
        # Tavily returns extracted page text, not just a snippet, so a
        # hit is often usable without a follow-up read_url call.
        return {
            "abstract": clean_snippet(item.get("content")),
            "published_date": item.get("published_date") or "",
            "score": item.get("score"),
        }

    results = data.get("results") if isinstance(data, dict) else None
    return _normalize_results(results, max_results, "tavily", fields)


@non_raising
async def search_brave(query: str, max_results: int, recency_days: int) -> dict[str, Any]:
    params: dict[str, str] = {
        "q": query,
        "count": str(max_results),
    }
    freshness = _brave_freshness(recency_days)
    if freshness:
        params["freshness"] = freshness
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": os.environ.get("BRAVE_API_KEY", ""),
    }
    try:
        async with make_client(_REQUEST_TIMEOUT) as client:
            resp = await client.get(_BRAVE_URL, params=params, headers=headers)
            resp.raise_for_status()
            results = normalize_brave(resp.json(), max_results)
    except (httpx.HTTPError, ValueError) as exc:
        return _handle_provider_error("brave", query, exc)
    _clear_credential_error("brave")
    return keyed_records(results)


@non_raising
async def search_tavily(query: str, max_results: int, recency_days: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }
    if recency_days > 0:
        payload["days"] = recency_days
    headers = {
        "Authorization": f"Bearer {os.environ.get('TAVILY_API_KEY', '')}",
        "Content-Type": "application/json",
    }
    try:
        async with make_client(_REQUEST_TIMEOUT) as client:
            resp = await client.post(_TAVILY_URL, json=payload, headers=headers)
            resp.raise_for_status()
            results = normalize_tavily(resp.json(), max_results)
    except (httpx.HTTPError, ValueError) as exc:
        return _handle_provider_error("tavily", query, exc)
    _clear_credential_error("tavily")
    return keyed_records(results)


SearchFn = Callable[[str, int, int], Awaitable[dict[str, Any]]]

_PROVIDERS: dict[str, tuple[SearchFn, str]] = {
    "brave": (search_brave, "BRAVE_API_KEY"),
    "tavily": (search_tavily, "TAVILY_API_KEY"),
}

# Default provider order favors Brave latency and its independent search index.
_AUTODETECT_ORDER = ("brave", "tavily")


def _resolve_requested_provider(requested: str) -> tuple[str, SearchFn] | None:
    entry = _PROVIDERS.get(requested)
    if entry is None:
        logger.warning(
            "Unknown WEB_SEARCH_PROVIDER %r, falling back to autodetect",
            requested,
        )
        return None
    if os.environ.get(entry[1]):
        return requested, entry[0]
    logger.warning("WEB_SEARCH_PROVIDER=%s but %s is not set", requested, entry[1])
    return None


def configured_providers() -> list[tuple[str, SearchFn]]:
    """Provider preference orders keys; additional keys are fallbacks rather
    than mutually exclusive choices.
    """
    ordered: list[str] = []
    requested = os.environ.get("WEB_SEARCH_PROVIDER", "").strip().lower()
    if requested and _resolve_requested_provider(requested) is not None:
        ordered.append(requested)
    ordered += [name for name in _AUTODETECT_ORDER if name not in ordered]
    return [
        (name, _PROVIDERS[name][0])
        for name in ordered
        if name in _PROVIDERS and os.environ.get(_PROVIDERS[name][1])
    ]


def candidate_providers() -> list[tuple[str, SearchFn]]:
    """Retry the preferred provider after universal refusal so monthly resets
    can become observable.
    """
    configured = configured_providers()
    healthy = [entry for entry in configured if _refusal_lifted(entry[0])]
    return healthy or configured[:1]


def resolve_provider() -> tuple[str, SearchFn] | None:
    candidates = candidate_providers()
    return candidates[0] if candidates else None


_MAX_RESULTS_CEILING = 20


@non_raising
async def search_web(
    query: str,
    max_results: int = 10,
    recency_days: int = 0,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Search the web and return ``{result_id: metadata}``.

    Args:
        query: Natural-language search query. Boolean operators are not
            interpreted by web engines the way they are by PubMed.
        max_results: Maximum number of results to return.
        recency_days: If > 0, restrict to results published within this many
            days.
        run_id: Unused; accepted for interface parity with other search
            tools.

    Returns:
        A dict keyed by result id, each value carrying title, url, abstract
        (the result snippet or extracted page text), source, and
        published_date. Empty on any error so a failed search degrades
        gracefully.
    """
    candidates = candidate_providers()
    if not candidates:
        logger.warning("Web search requested but no provider key is configured")
        return failed("no web provider configured")

    capped = min(max(max_results, 1), _MAX_RESULTS_CEILING)
    throttled = None
    for name, search_fn in candidates:
        results = await search_fn(query, capped, max(recency_days, 0))
        if results.get("status") == "ok":
            logger.debug(
                "web search via %s returned %s results",
                name,
                len(results["records"]),
            )
            return results
        if credential_error_for(name) is not None:
            logger.warning("%s refused the search; trying the next provider", name)
        elif results.get("error") == _RATE_LIMITED:
            throttled = results
            logger.warning("%s is rate limited; trying the next provider for this query", name)
        else:
            # Empty success is an answer; do not spend another allowance to
            # hear it twice.
            logger.debug("web search via %s returned no results", name)
            return results
    return throttled or failed("all web providers refused the search")


async def check_web_search_available() -> bool:
    """Reports whether a web search issued now would reach a provider.

    The connector's status used to be inferred from whether the server
    advertises ``search_web`` at all, which only tells you that some key
    was set at boot. A key that the provider has since refused -- revoked,
    unpaid, or out of quota -- leaves the tool registered and answering
    every search with an empty result set, so the connector reads as
    healthy while returning nothing. This answers the question that was
    actually being asked.

    Returns:
        True when at least one configured provider has not been refused
        since the last search that worked, or its quota reset has passed.
    """
    return any(_refusal_lifted(name) for name, _ in configured_providers())
