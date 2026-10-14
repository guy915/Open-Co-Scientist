"""bioRxiv's own API lists posting dates, not subject queries; Europe PMC
supplies subject search.
"""

import asyncio
import logging
from typing import Any

import httpx

from mcp_server.http_client import make_client
from mcp_server.log_privacy import failure_summary
from mcp_server.text_extraction import clean_markup
from mcp_server.tools._results import failed, non_raising, ok

logger = logging.getLogger(__name__)

_EUROPEPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Europe PMC's source code for preprint servers (bioRxiv, medRxiv, and
# the other servers it indexes).
_PREPRINT_FILTER = "SRC:PPR"

# Europe PMC's PUBLISHER filter restricts bioRxiv independently of its broader
# preprint source filter.
_BIORXIV_FILTER = 'PUBLISHER:"bioRxiv"'

# Idle keep-alive drops warrant a fresh transport attempt; HTTP error answers do
# not.
_TRANSPORT_RETRY_DELAYS_SECONDS = (0.5, 1.5)


def _results(payload: Any) -> list[dict[str, Any]]:
    """Require a result list so a broken response cannot imply no matches."""
    result_list = payload.get("resultList") if isinstance(payload, dict) else None
    results = result_list.get("result") if isinstance(result_list, dict) else None
    if not isinstance(results, list) or any(not isinstance(record, dict) for record in results):
        raise ValueError("invalid Europe PMC resultList.result")
    return results


def _record(result: dict[str, Any]) -> dict[str, Any]:
    doi = result.get("doi")
    pmid = result.get("pmid")
    # Source/ID pairs survive missing DOIs and prevent cross-query list-position
    # collisions.
    record_id = f"{result.get('source', 'MED')}/{result.get('id')}"
    return {
        "source_id": record_id,
        # Europe PMC italicizes species and gene names, and sends that
        # markup escaped on some records and raw on others.
        "title": clean_markup(result.get("title")),
        "abstract": clean_markup(result.get("abstractText")),
        "year": result.get("pubYear"),
        "journal": (result.get("journalInfo") or {}).get("journal", {}).get("title")
        or result.get("bookOrReportDetails", {}).get("publisher"),
        "authors": result.get("authorString"),
        "doi": doi,
        "pmid": pmid,
        # Whether this is peer reviewed is a fact about the evidence, not
        # a detail of the record: a preprint supporting a claim is a
        # weaker citation and the model has to be able to say so.
        "is_preprint": result.get("source") == "PPR",
        "cited_by_count": result.get("citedByCount"),
        "url": (f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{record_id}"),
    }


async def _get_with_transport_retry(
    params: dict[str, str | int],
) -> httpx.Response:
    for delay in (*_TRANSPORT_RETRY_DELAYS_SECONDS, None):
        try:
            async with make_client(30) as client:
                response = await client.get(_EUROPEPMC_URL, params=params)
                response.raise_for_status()
            return response
        except httpx.TransportError:
            if delay is None:
                raise
            logger.info(
                "Europe PMC connection failed; retrying in %.1fs",
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("transport retry loop exited without a result")


async def _search(query: str, max_results: int) -> dict[str, Any]:
    limit = max(1, min(max_results, 25))
    params: dict[str, str | int] = {
        "query": query,
        "format": "json",
        "pageSize": limit,
        "resultType": "core",
        # Citation sorting buries recent preprints; relevance balances recency
        # and topic.
        "sort": "",
    }
    try:
        response = await _get_with_transport_retry(params)
        records = [_record(result) for result in _results(response.json())[:limit]]
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Europe PMC search failed (%s)", failure_summary(exc))
        return failed(exc)
    return ok(records)


@non_raising
async def search_europepmc(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search Europe PMC's full corpus of papers and preprints.

    Args:
        query: Free-text or Europe PMC query syntax.
        max_results: Maximum records to return, capped at 25.

    Returns:
        Source-stamped paper records, each flagged as preprint or not, or
        an empty-records envelope.
    """
    return await _search(query, max_results)


@non_raising
async def search_preprints(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search bioRxiv, medRxiv and other preprint servers.

    Args:
        query: Free-text or Europe PMC query syntax.
        max_results: Maximum records to return, capped at 25.

    Returns:
        Source-stamped preprint records, or an empty-records envelope.
    """
    return await _search(
        f"({query}) AND {_PREPRINT_FILTER}",
        max_results,
    )


@non_raising
async def search_biorxiv(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search bioRxiv specifically, not the broader preprint set above.

    bioRxiv's own REST API lists papers by posting date and cannot be
    queried by subject at all (see the module docstring), so a genuine
    bioRxiv subject search has to run through Europe PMC too, restricted
    to this one publisher rather than every preprint server it indexes.

    Args:
        query: Free-text or Europe PMC query syntax.
        max_results: Maximum records to return, capped at 25.

    Returns:
        Source-stamped bioRxiv records, or an empty-records envelope.
    """
    return await _search(
        f"({query}) AND {_PREPRINT_FILTER} AND {_BIORXIV_FILTER}",
        max_results,
    )
