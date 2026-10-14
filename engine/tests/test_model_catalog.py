from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from io import StringIO
from typing import Any

import httpx
import pytest

from co_scientist.core.byok_scope import ByokRedactionFilter, current_byok
from co_scientist.platform.llm import model_catalog as catalog

_CREDENTIAL = "catalog-fixture-credential"
_HTTP_CLIENT = httpx.AsyncClient


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay: float = 0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False
        self.read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return _HTTP_CLIENT(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client)


def response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, stream=Body([json.dumps(payload).encode()]))


@pytest.mark.parametrize("provider", catalog._ENDPOINTS)
def test_provider_lists_use_fixed_hosts_and_header_credentials(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response({"models" if provider == "gemini" else "data": [{"id": "model"}]})

    install_transport(monkeypatch, handle)
    assert catalog.read_provider_models(provider, _CREDENTIAL) == [{"id": "model"}]
    (request,) = requests
    assert str(request.url) == catalog._ENDPOINTS[provider]
    assert _CREDENTIAL not in str(request.url)
    header = {"anthropic": "x-api-key", "gemini": "x-goog-api-key"}.get(provider, "authorization")
    assert _CREDENTIAL in request.headers[header]
    assert request.headers["accept-encoding"] == "identity"


def test_redirects_never_forward_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    install_transport(monkeypatch, handle)
    with pytest.raises(catalog.ModelCatalogError):
        catalog.read_provider_models("openai", _CREDENTIAL)
    assert len(requests) == 1


def test_pagination_token_never_selects_a_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    token = "http://127.0.0.1/private"

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response({"models": [], **({"nextPageToken": token} if len(requests) == 1 else {})})

    install_transport(monkeypatch, handle)
    assert catalog.read_provider_models("gemini", _CREDENTIAL) == []
    assert all(request.url.host == "generativelanguage.googleapis.com" for request in requests)
    assert requests[1].url.params["pageToken"] == token


def test_unknown_provider_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("unexpected outbound request")

    install_transport(monkeypatch, handle)
    with pytest.raises(catalog.ModelCatalogError, match="Unsupported provider"):
        catalog.read_provider_models("http://127.0.0.1", _CREDENTIAL)


def test_oversized_chunked_response_is_closed_before_further_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "_MAX_BYTES", 16)
    body = Body([b"x" * 17, b"must never be read"])
    install_transport(monkeypatch, lambda _: httpx.Response(200, stream=body))
    with pytest.raises(catalog.ModelCatalogError):
        catalog.read_provider_models("openai", _CREDENTIAL)
    assert body.read == 1
    assert body.closed


def test_declared_oversize_and_compression_are_refused_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for headers in ({"Content-Length": str(catalog._MAX_BYTES + 1)}, {"Content-Encoding": "gzip"}):
        body = Body([b"must never be read"])

        def handle(
            _: httpx.Request, *, h: dict[str, str] = headers, b: Body = body
        ) -> httpx.Response:
            return httpx.Response(200, headers=h, stream=b)

        install_transport(monkeypatch, handle)
        with pytest.raises(catalog.ModelCatalogError):
            catalog.read_provider_models("openai", _CREDENTIAL)
        assert body.read == 0
        assert body.closed


def test_page_bytes_share_one_total_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    data = json.dumps({"models": [], "nextPageToken": "again"}).encode()
    monkeypatch.setattr(catalog, "_MAX_BYTES", len(data) * 2 - 1)
    bodies: list[Body] = []

    def handle(_: httpx.Request) -> httpx.Response:
        body = Body([data])
        bodies.append(body)
        return httpx.Response(200, stream=body)

    install_transport(monkeypatch, handle)
    with pytest.raises(catalog.ModelCatalogError):
        catalog.read_provider_models("gemini", _CREDENTIAL)
    assert len(bodies) == 2
    assert all(body.closed for body in bodies)


def test_rows_and_pages_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "_MAX_ROWS", 1)
    install_transport(monkeypatch, lambda _: response({"data": [{"id": "a"}, {"id": "b"}]}))
    with pytest.raises(catalog.ModelCatalogError):
        catalog.read_provider_models("openai", _CREDENTIAL)
    monkeypatch.setattr(catalog, "_MAX_PAGES", 2)
    install_transport(monkeypatch, lambda _: response({"models": [], "nextPageToken": "again"}))
    with pytest.raises(catalog.ModelCatalogError):
        catalog.read_provider_models("gemini", _CREDENTIAL)


def test_slow_drip_is_cancelled_by_the_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "_TOTAL_TIMEOUT", 0.03)
    body = Body([b" " for _ in range(100)], delay=0.01)
    install_transport(monkeypatch, lambda _: httpx.Response(200, stream=body))
    start = time.monotonic()
    with pytest.raises(catalog.ModelCatalogTimeoutError):
        catalog.read_provider_models("openai", _CREDENTIAL)
    assert time.monotonic() - start < 1
    assert body.read < 100
    assert body.closed


def test_provider_errors_cannot_expose_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.RequestError(_CREDENTIAL, request=request)

    install_transport(monkeypatch, handle)
    with pytest.raises(catalog.ModelCatalogError) as error:
        catalog.read_provider_models("openai", _CREDENTIAL)
    assert _CREDENTIAL not in str(error.value) + caplog.text


def test_provider_list_logs_use_the_credential_redaction_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(ByokRedactionFilter())
    logger = logging.getLogger("catalog-redaction-test")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)

    def handle(_: httpx.Request) -> httpx.Response:
        credential = current_byok()
        assert credential is not None and credential.api_key == _CREDENTIAL
        logger.warning("Provider diagnostic echoed %s", _CREDENTIAL)
        return response({"data": []})

    install_transport(monkeypatch, handle)
    try:
        assert catalog.read_provider_models("openai", _CREDENTIAL) == []
        assert _CREDENTIAL not in output.getvalue()
        assert "[REDACTED]" in output.getvalue()
        assert current_byok() is None
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize("provider", ["gemini", "anthropic"])
def test_provider_cannot_put_the_credential_into_pagination_urls(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload: dict[str, Any] = {"models": [], "nextPageToken": _CREDENTIAL}
        if provider == "anthropic":
            payload = {"data": [], "has_more": True, "last_id": _CREDENTIAL}
        return response(payload)

    install_transport(monkeypatch, handle)
    with pytest.raises(catalog.ModelCatalogError):
        catalog.read_provider_models(provider, _CREDENTIAL)
    assert len(requests) == 1
    assert _CREDENTIAL not in str(requests[0].url)
