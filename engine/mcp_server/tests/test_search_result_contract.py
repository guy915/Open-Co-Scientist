from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp_server.tools.biomedical_databases import search_clinical_trials, search_ensembl_gene
from mcp_server.tools.lit_review.arxiv_search import search_arxiv
from mcp_server.tools.lit_review.europepmc_search import search_europepmc
from mcp_server.tools.lit_review.openalex_search import search_openalex
from mcp_server.tools.web_providers import search_brave


@pytest.mark.parametrize(
    "tool",
    [
        search_clinical_trials,
        search_ensembl_gene,
        search_arxiv,
        search_europepmc,
        search_openalex,
    ],
)
async def test_backend_outage_returns_failed_not_empty_success(
    tool: Callable[..., Awaitable[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mcp_server.tools.lit_review.europepmc_search._TRANSPORT_RETRY_DELAYS_SECONDS",
        (0.0, 0.0),
    )
    with patch("httpx.AsyncClient.__aenter__", side_effect=httpx.ConnectError("down")) as attempts:
        result = await tool("weak claim")
    assert attempts.call_count == (3 if tool is search_europepmc else 1)
    assert result == {"status": "failed", "records": [], "error": "network_error"}


async def test_web_backend_outage_returns_failed() -> None:
    with patch("httpx.AsyncClient.__aenter__", side_effect=httpx.ConnectError("down")):
        result = await search_brave("weak claim", 3, 0)
    assert result == {"status": "failed", "records": [], "error": "network_error"}
