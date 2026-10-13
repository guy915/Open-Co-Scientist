from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from mcp_server.tests._entrez import install_entrez
from mcp_server.tests._httpx import registered_tools, transport_responses
from mcp_server.tools import web_providers
from mcp_server.tools.lit_review import search_pubmed
from mcp_server.tools.web_fetch import UrlNotFetchableError, read_url


@pytest.fixture(autouse=True)
def _fake_transport_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # These cases assert error envelopes, not elapsed retry pacing.
    monkeypatch.setattr(
        "mcp_server.tools.lit_review.europepmc_search._TRANSPORT_RETRY_DELAYS_SECONDS",
        (0.0, 0.0),
    )


_RECORD_TOOLS = [
    ("search_chembl", {"query": "aspirin"}, "ChEMBL"),
    ("search_uniprot", {"query": "EGFR"}, "UniProtKB/Swiss-Prot"),
    ("search_clinical_trials", {"query": "imatinib"}, "ClinicalTrials.gov"),
    ("search_ensembl_gene", {"query": "WEE1"}, "Ensembl"),
    ("search_gnomad_constraint", {"query": "WEE1"}, "gnomAD"),
    ("search_string_interactions", {"query": "WEE1"}, "STRING"),
    ("search_reactome_pathways", {"query": "WEE1"}, "Reactome"),
    ("search_open_targets", {"query": "WEE1"}, "Open Targets"),
    ("search_europepmc", {"query": "WEE1"}, "Europe PMC"),
    ("search_preprints", {"query": "WEE1"}, "Preprints"),
    ("search_biorxiv", {"query": "WEE1"}, "bioRxiv"),
    ("search_arxiv", {"query": "WEE1"}, "arXiv"),
]


def _assert_error(error: Any, kind: str) -> None:
    assert isinstance(error, str) and error
    expected = {
        "http_status": "HTTP",
        "invalid_request": "invalid_response",
        "blocked": "blocked",
        "unavailable": "invalid_response",
    }.get(kind, kind)
    assert expected in error


@pytest.mark.parametrize(("tool_name", "arguments", "source"), _RECORD_TOOLS)
async def test_an_upstream_refusal_is_an_explicit_failed_result_not_a_raise_or_no_match(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, arguments: dict[str, Any], source: str
) -> None:
    transport_responses(monkeypatch, *[httpx.Response(503, json={})] * 3)

    async with registered_tools() as client:
        result = await client.call_tool(tool_name, arguments, raise_on_error=False)

    assert result.is_error is not True
    assert result.data["status"] == "failed"
    assert result.data["records"] == []
    _assert_error(result.data["error"], "http_status")
    assert "503" in result.data["error"]


@pytest.mark.parametrize(("tool_name", "arguments", "source"), _RECORD_TOOLS)
async def test_an_unreachable_upstream_is_an_explicit_failed_result(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, arguments: dict[str, Any], source: str
) -> None:
    transport_responses(monkeypatch, *[httpx.ConnectError("connection refused")] * 3)

    async with registered_tools() as client:
        result = await client.call_tool(tool_name, arguments, raise_on_error=False)

    assert result.is_error is not True
    assert result.data["records"] == []
    _assert_error(result.data["error"], "network_error")


async def test_an_unknown_gene_symbol_is_an_empty_answer_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_responses(monkeypatch, httpx.Response(404, json={"error": "No valid lookup"}))

    async with registered_tools() as client:
        result = await client.call_tool("search_ensembl_gene", {"query": "NOTAGENE"})

    assert result.data == {"status": "ok", "records": []}


async def test_openalex_failure_is_an_explicit_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_responses(monkeypatch, *[httpx.Response(503, json={})] * 3)

    async with registered_tools() as client:
        result = await client.call_tool("search_openalex", {"query": "WEE1"}, raise_on_error=False)

    assert result.is_error is not True
    _assert_error(result.data["error"], "http_status")
    assert set(result.data) == {"status", "records", "error"}


async def test_citation_edge_failures_are_explicit_failed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_responses(monkeypatch, httpx.Response(503, json={}))

    async with registered_tools() as client:
        outage = await client.call_tool(
            "get_opencitations_citation_edges", {"doi": "10.1234/example"}, raise_on_error=False
        )
        invalid = await client.call_tool(
            "get_opencitations_citation_edges", {"doi": "not a doi"}, raise_on_error=False
        )

    assert outage.is_error is not True
    assert outage.data["status"] == "failed"
    _assert_error(outage.data["error"], "http_status")
    assert invalid.is_error is not True
    _assert_error(invalid.data["error"], "invalid_request")


async def test_pubmed_failures_are_explicit_failed_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    def refuse(**_kwargs: Any) -> Any:
        raise RuntimeError("Search Backend failed")

    install_entrez(monkeypatch, esearch=refuse)
    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))

    searched = search_pubmed.search_pubmed("WEE1")
    fulltext = await search_pubmed.pubmed_search_with_fulltext("WEE1", slug="goal")
    invalid = await search_pubmed.pubmed_search_with_fulltext("WEE1", slug="../escape")

    assert searched["status"] == "failed" and searched["records"] == []
    _assert_error(searched["error"], "unavailable")
    _assert_error(fulltext["error"], "unavailable")
    _assert_error(invalid["error"], "invalid_request")


async def test_a_web_search_outage_is_an_explicit_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    transport_responses(monkeypatch, httpx.Response(429, json={}))

    result = await web_providers.search_web("WEE1")

    _assert_error(result["error"], "http_status")
    assert "429" in result["error"]


@pytest.mark.parametrize(
    ("url", "response", "kind"),
    [
        ("http://127.0.0.1/admin", None, "blocked"),
        ("https://example.com/missing", httpx.Response(404), "http_status"),
        ("https://example.com/down", httpx.ConnectError("refused"), "network_error"),
    ],
)
async def test_an_unreadable_url_is_an_explicit_failed_result(
    monkeypatch: pytest.MonkeyPatch, url: str, response: Any, kind: str
) -> None:
    monkeypatch.setattr(
        "mcp_server.tools.web_fetch.check_fetchable",
        lambda target: None if urlsplit(target).hostname == "example.com" else _refuse(target),
    )
    if response is not None:
        monkeypatch.setattr(
            "mcp_server.safe_http.validate_http_url",
            lambda target: ("example.com", "93.184.216.34"),
        )
        requests = transport_responses(monkeypatch, response)

    result = await read_url(url)

    assert result["status"] == "failed"
    _assert_error(result["error"], kind)
    if response is not None:
        assert len(requests) == 1


def _refuse(target: str) -> None:
    raise UrlNotFetchableError(f"host not allowed: {target}")


@pytest.mark.parametrize(
    ("result", "described"),
    [
        (
            {"status": "failed", "records": [], "error": "timeout"},
            "failed",
        ),
        ({"source": "S", "query": "q", "records": []}, "empty"),
        ({"source": "S", "query": "q", "records": [{}, {}]}, "2 items"),
    ],
)
def test_call_logs_tell_a_failed_lookup_from_an_empty_one(result: Any, described: str) -> None:
    from mcp_server.tool_logging import _describe_result

    assert _describe_result(result) == described
