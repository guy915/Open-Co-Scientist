from __future__ import annotations

import socket
from collections.abc import Iterator

import httpx
import pytest
from co_scientist.platform.retrieval import pinned_http


def _dns(_host: str, _port: object) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def test_pinned_transport_dials_ip_and_preserves_tls_name_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    with httpx.Client(transport=httpx.MockTransport(capture)) as client:
        pinned_http.request_with_screened_redirects(client, "HEAD", "https://example.org/paper")

    (request,) = requests
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "example.org"
    assert request.extensions["sni_hostname"] == "example.org"


def test_a_pinned_probe_is_attributed_to_each_validated_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)
    sources: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sources.append((request.extensions["source_host"], request.extensions["source_url"]))
        if request.headers["host"] == "doi.org":
            location = "https://publisher.example/article"
            return httpx.Response(302, headers={"location": location}, request=request)
        return httpx.Response(200, request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        response = pinned_http.request_with_screened_redirects(
            client, "HEAD", "https://doi.org/10.1/x"
        )

    assert sources == [
        ("doi.org", "https://doi.org/10.1/x"),
        ("publisher.example", "https://publisher.example/article"),
    ]
    assert response.request.extensions["source_host"] == "publisher.example"


def test_redirect_private_hop_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(redirect)) as client,
        pytest.raises(pinned_http.UnsafeUrlError, match="non-public"),
    ):
        pinned_http.request_with_screened_redirects(client, "HEAD", "https://example.org/")


@pytest.mark.parametrize("first_status", [302, 403, 405])
def test_citation_probe_closes_every_hop_without_consuming_response_bodies(
    monkeypatch: pytest.MonkeyPatch, first_status: int
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _dns)
    streams: list[_UnreadBody] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        stream = _UnreadBody()
        streams.append(stream)
        status = first_status if len(requests) == 1 else 200
        headers = {"location": "/paper"} if status == 302 else {}
        return httpx.Response(status, headers=headers, stream=stream, request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        response = pinned_http.request_with_screened_redirects(
            client, "HEAD", "https://example.org/"
        )

    assert response.status_code == 200
    assert all(stream.closed for stream in streams)
    assert requests[-1].method == ("HEAD" if first_status == 302 else "GET")


class _UnreadBody(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        raise AssertionError("a status-only probe consumed an unbounded response body")
        yield b""

    def close(self) -> None:
        self.closed = True
