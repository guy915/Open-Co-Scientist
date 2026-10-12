import logging
import urllib.error
from collections.abc import Awaitable, Callable
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from Bio import Entrez
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp_server import entrez, text_extraction
from mcp_server.literature_review import PubmedSource
from mcp_server.log_privacy import failure_summary, install_transport_log_privacy
from mcp_server.tool_logging import with_call_logging
from mcp_server.tools import biomedical_databases, web_fetch, web_providers
from mcp_server.tools.lit_review import (
    arxiv_search,
    europepmc_search,
    openalex_search,
    opencitations,
    search_pubmed,
)
from pydantic import ValidationError

_PRIVATE = "PRIVATE_QUERY PRIVATE_DOCUMENT person@example.test PRIVATE_CREDENTIAL"
_URL = "https://example.test/paper?credential=PRIVATE_CREDENTIAL&query=PRIVATE_QUERY"


def _assert_private_absent(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records
    for marker in _PRIVATE.split():
        assert marker not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_real_empty_search_and_query_normalization_keep_private_arguments_out_of_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(PubmedSource, "_esearch_ids", Mock(return_value=[]))
    search = with_call_logging(PubmedSource(tmp_path).pubmed_search_ids, "pubmed")
    with caplog.at_level(logging.DEBUG, logger="mcp_server"):
        assert search(_PRIVATE) == []
        assert openalex_search._sanitize_query(_PRIVATE + "*") == _PRIVATE
    assert "returned no results" in caplog.text
    assert "Normalized OpenAlex query syntax" in caplog.text
    assert "1 positional, 0 named" in caplog.text
    _assert_private_absent(caplog)


@pytest.mark.parametrize("source", ["chembl", "openalex", "europepmc"])
async def test_actual_provider_failure_paths_drop_query_url_and_error_payloads(
    source: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    request = httpx.Request("GET", _URL)
    response = httpx.Response(503, request=request, json={"message": _PRIVATE})
    error = httpx.HTTPStatusError(_PRIVATE, request=request, response=response)
    call: Callable[[str], Awaitable[dict[str, Any]]]
    if source == "chembl":
        monkeypatch.setattr(biomedical_databases, "_get_json", AsyncMock(side_effect=error))
        call = biomedical_databases.search_chembl
    elif source == "openalex":
        monkeypatch.setattr(
            openalex_search, "_collect_openalex_works", AsyncMock(side_effect=error)
        )
        call = openalex_search.search_openalex
    else:
        monkeypatch.setattr(
            europepmc_search, "_get_with_transport_retry", AsyncMock(side_effect=error)
        )
        call = europepmc_search.search_europepmc
    with caplog.at_level(logging.DEBUG, logger="mcp_server"):
        result = await call(_PRIVATE)
    assert result == {"status": "failed", "records": [], "error": "HTTP 503"}
    assert "(HTTPStatusError, HTTP 503)" in caplog.text
    _assert_private_absent(caplog)


@pytest.mark.parametrize("failure", ["screen", "redirect", "status", "transport"])
async def test_url_fetch_failures_keep_numeric_status_but_never_the_private_url(
    failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error: Exception
    if failure in {"screen", "redirect"}:
        error = web_fetch.UrlNotFetchableError(_PRIVATE)
    elif failure == "status":
        request = httpx.Request("GET", _URL)
        error = httpx.HTTPStatusError(
            _PRIVATE, request=request, response=httpx.Response(503, request=request)
        )
    else:
        error = httpx.ConnectError(_PRIVATE)
    monkeypatch.setattr(
        web_fetch, "check_fetchable", Mock(side_effect=error if failure == "screen" else None)
    )
    monkeypatch.setattr(web_fetch, "_fetch_and_render", AsyncMock(side_effect=error))
    with caplog.at_level(logging.DEBUG, logger="mcp_server"):
        result = await web_fetch.read_url(_URL)
    assert result["status"] == "failed" and result["records"] == []
    if failure == "status":
        assert "HTTP 503" in caplog.text and result["error"] == "HTTP 503"
    _assert_private_absent(caplog)


async def test_metadata_fulltext_and_availability_errors_do_not_emit_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = PubmedSource(tmp_path)
    directory, _ = source._prepare_run_directories("PRIVATE_TOPIC", "PRIVATE_RUN")
    monkeypatch.setattr(source, "_fetch_papers_details", Mock(side_effect=RuntimeError(_PRIVATE)))
    monkeypatch.setattr(source, "_download_pmc_fulltext", Mock(side_effect=RuntimeError(_PRIVATE)))
    monkeypatch.setattr(search_pubmed, "entrez_call", Mock(side_effect=RuntimeError(_PRIVATE)))
    with caplog.at_level(logging.DEBUG, logger="mcp_server"):
        assert await source._gather_paper_metadata(["123"], directory) == ({}, 0)
        assert source.get_pubmed_fulltext("123", "PRIVATE_TOPIC", "PRIVATE_RUN") is None
        assert search_pubmed.check_pubmed_available() == "false"
    _assert_private_absent(caplog)


def test_entrez_parser_failure_closes_handle_without_logging_the_private_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    handle = Mock()
    monkeypatch.setattr(Entrez, "read", Mock(side_effect=RuntimeError(_PRIVATE)))
    with caplog.at_level(logging.DEBUG, logger="mcp_server"), pytest.raises(RuntimeError):
        entrez.read_entrez(handle)
    handle.close.assert_called_once()
    _assert_private_absent(caplog)


def test_parser_fallbacks_keep_errors_private_and_preserve_the_failure_placeholder(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(text_extraction, "BeautifulSoup", Mock(side_effect=RuntimeError(_PRIVATE)))
    monkeypatch.setattr(web_fetch, "BeautifulSoup", Mock(side_effect=RuntimeError(_PRIVATE)))
    with caplog.at_level(logging.DEBUG, logger="mcp_server"):
        assert text_extraction.extract_text_from_pmc_html(_PRIVATE).startswith("[error:")
        assert web_fetch.extract_text_from_html(_PRIVATE).startswith("[error:")
    _assert_private_absent(caplog)


async def test_actual_httpx_request_logging_omits_query_strings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_transport_log_privacy()
    with caplog.at_level(logging.DEBUG, logger="httpx"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        ) as client:
            assert (await client.get(_URL)).status_code == 200
    assert "MCP transport diagnostic" in caplog.text
    _assert_private_absent(caplog)


@pytest.mark.parametrize("source", ["arxiv", "opencitations", "brave"])
async def test_remaining_search_sources_preserve_refusal_without_private_diagnostics(
    source: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, json={"message": _PRIVATE})

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(web_providers, "_credential_errors", {})
    with caplog.at_level(logging.DEBUG, logger="mcp_server"):
        if source == "arxiv":
            monkeypatch.setattr(arxiv_search, "make_client", client)
            result = await arxiv_search.search_arxiv(_PRIVATE)
        elif source == "opencitations":
            monkeypatch.setattr(opencitations, "make_client", client)
            result = await opencitations.get_opencitations_citation_edges("10.1234/PRIVATE_QUERY")
        else:
            monkeypatch.setattr(web_providers, "make_client", client)
            monkeypatch.setenv("BRAVE_API_KEY", "PRIVATE_CREDENTIAL")
            result = await web_providers.search_brave(_PRIVATE, 5, 0)
    assert result == {"status": "failed", "records": [], "error": "HTTP 503"}
    _assert_private_absent(caplog)


async def test_actual_framework_exception_and_validation_logs_are_private(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    server = FastMCP("privacy-test")

    @server.tool
    def failing_tool(value: int) -> dict[str, Any]:
        raise RuntimeError(_PRIVATE)

    install_transport_log_privacy()
    with pytest.raises(ToolError):
        await server.call_tool("failing_tool", {"value": 1})
    with pytest.raises(ValidationError):
        await server.call_tool("failing_tool", {"value": _PRIVATE})
    rendered = caplog.text + capsys.readouterr().err
    assert "MCP transport diagnostic" in rendered
    assert all(marker not in rendered for marker in _PRIVATE.split())


async def test_unauthenticated_health_retains_refusal_status_without_previous_search_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_server.auth_middleware import SharedSecretAuthMiddleware
    from mcp_server.server import app

    monkeypatch.setattr(web_providers, "_credential_errors", {})
    request = httpx.Request("GET", _URL)
    response = httpx.Response(402, request=request)
    with pytest.raises(httpx.HTTPStatusError) as refused:
        response.raise_for_status()
    result = web_providers._handle_provider_error("brave", _PRIVATE, refused.value)
    assert result == {"status": "failed", "records": [], "error": "HTTP 402"}
    secured = SharedSecretAuthMiddleware(app, secret="synthetic-mcp-test-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=secured), base_url="http://mcp.test"
    ) as client:
        health = await client.get("/")
    assert health.status_code == 200
    assert health.json()["integrations"]["web_search_credential_error"] == {
        "provider": "brave",
        "status": 402,
        "detail": "HTTP 402",
    }
    assert all(marker not in health.text for marker in _PRIVATE.split())


class PrivateQueryError(Exception):
    pass


def test_failure_summary_names_only_allowlisted_classes_and_numeric_status() -> None:
    request = httpx.Request("GET", _URL)
    status_error = httpx.HTTPStatusError(
        _PRIVATE, request=request, response=httpx.Response(429, request=request)
    )
    url_error = urllib.error.HTTPError(_URL, 400, _PRIVATE, Message(), None)

    assert failure_summary(status_error) == "HTTPStatusError, HTTP 429"
    assert failure_summary(url_error) == "HTTPError, HTTP 400"
    assert failure_summary(IndexError(_PRIVATE)) == "IndexError"
    assert failure_summary(httpx.ConnectTimeout(_PRIVATE)) == "ConnectTimeout"
    assert failure_summary(PrivateQueryError(_PRIVATE)) == "other error"


def test_a_pubmed_book_or_index_failure_is_named_in_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(search_pubmed, "initialize_entrez", lambda: None)
    monkeypatch.setattr(search_pubmed, "_esearch_pubmed_ids", lambda query, limit: ["1"])
    monkeypatch.setattr(
        search_pubmed, "fetch_pubmed_records", Mock(side_effect=IndexError(_PRIVATE))
    )
    with caplog.at_level(logging.DEBUG, logger="mcp_server"):
        search_pubmed.search_pubmed(_PRIVATE, 1)
    assert "PubMed metadata fetch failed (IndexError)" in caplog.text
    _assert_private_absent(caplog)
