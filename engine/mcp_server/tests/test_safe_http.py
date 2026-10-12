from __future__ import annotations

import asyncio
import gzip
import socket
import time
import zlib
from collections.abc import AsyncIterator

import httpx
import pytest
from mcp_server import safe_http


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, delay: float = 0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False
        self.read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _dns(_host: str, _port: object) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


async def test_pinned_transport_dials_ip_and_preserves_tls_name_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=_Chunks(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        await safe_http.get_with_screened_redirects(client, "https://example.org/paper")

    (request,) = requests
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "example.org"
    assert request.extensions["sni_hostname"] == "example.org"


@pytest.mark.parametrize("final_status", [200, 403])
async def test_a_pinned_fetch_is_attributed_to_each_validated_hostname(
    monkeypatch: pytest.MonkeyPatch, final_status: int
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)
    sources: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sources.append((request.extensions["source_host"], request.extensions["source_url"]))
        if request.headers["host"] == "doi.org":
            location = "https://publisher.example/article"
            return httpx.Response(302, headers={"location": location}, request=request)
        return httpx.Response(final_status, stream=_Chunks(b"ok"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await safe_http.get_with_screened_redirects(client, "https://doi.org/10.1/x")

    assert sources == [
        ("doi.org", "https://doi.org/10.1/x"),
        ("publisher.example", "https://publisher.example/article"),
    ]
    assert response.url.host == "93.184.216.34"
    assert response.extensions["source_host"] == "publisher.example"
    assert response.extensions["source_url"] == "https://publisher.example/article"


async def test_each_redirect_target_is_screened_and_private_hops_are_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(safe_http.UnsafeUrlError, match="non-public"):
            await safe_http.get_with_screened_redirects(client, "https://example.org/")


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({}, b"123456"),
        ({"content-length": "100"}, b"ok"),
        ({"content-encoding": "gzip"}, gzip.compress(b"x" * 100)),
    ],
)
async def test_response_limit_covers_chunked_declared_and_decompressed_bytes(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], body: bytes
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=_Chunks(body), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(safe_http.ResponseTooLargeError):
            await safe_http.get_with_screened_redirects(
                client, "https://example.org/paper", max_bytes=5
            )


async def test_gzip_members_are_decoded_and_truncated_streams_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)
    responses = [
        gzip.compress(b"first") + gzip.compress(b"second"),
        gzip.compress(b"truncated")[:-3],
    ]

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=_Chunks(responses.pop(0)),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await safe_http.get_with_screened_redirects(
            client, "https://example.org/paper", max_bytes=100
        )
        assert response.content == b"firstsecond"
        with pytest.raises(httpx.DecodingError):
            await safe_http.get_with_screened_redirects(
                client, "https://example.org/paper", max_bytes=100
            )


async def test_raw_deflate_remains_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    body = compressor.compress(b"raw deflate") + compressor.flush()

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "deflate"},
            stream=_Chunks(body),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await safe_http.get_with_screened_redirects(
            client, "https://example.org/paper", max_bytes=100
        )
    assert response.content == b"raw deflate"


async def test_redirect_body_is_closed_without_being_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)
    redirect_body = _Chunks(b"do not read")
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                302,
                headers={"location": "/landed"},
                stream=redirect_body,
                request=request,
            )
        return httpx.Response(200, stream=_Chunks(b"landed"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await safe_http.get_with_screened_redirects(
            client, "https://example.org/start", max_bytes=20
        )

    assert response.content == b"landed"
    assert redirect_body.closed is True
    assert redirect_body.read is False


async def test_deadline_covers_slow_streaming_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunks(b"ok", delay=0.05), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.TimeoutException):
            await safe_http.get_with_screened_redirects(
                client,
                "https://example.org/paper",
                max_bytes=20,
                timeout_seconds=0.01,
            )


async def test_deadline_does_not_wait_for_redirect_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_dns(_host: str, _port: object) -> list[tuple[object, ...]]:
        time.sleep(0.1)
        return _dns(_host, _port)

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    started = time.monotonic()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    ) as client:
        with pytest.raises(httpx.TimeoutException):
            await safe_http.get_with_screened_redirects(
                client,
                "https://example.org/paper",
                timeout_seconds=0.01,
            )
    assert time.monotonic() - started < 0.08
