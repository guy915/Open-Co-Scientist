from __future__ import annotations

import io
import socket
import ssl
import threading
import time
import urllib.request
from http.client import HTTPResponse, responses
from typing import cast
from urllib.parse import parse_qs, urlsplit
from urllib.request import BaseHandler, Request, build_opener

import mcp_server.entrez as entrez_rate_limit
import pytest
from Bio import Entrez
from Bio.Entrez import Parser
from mcp_server import entrez


def test_entrez_rejects_insecure_tls_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entrez, "_entrez_initialized", False)
    monkeypatch.setenv("DISABLE_SSL_VERIFY", "true")
    original = ssl._create_default_https_context

    with pytest.raises(RuntimeError, match="TLS verification"):
        entrez.initialize_entrez()

    assert ssl._create_default_https_context is original
    assert entrez._entrez_initialized is False


def _capture_requests(monkeypatch: pytest.MonkeyPatch) -> list[Request]:
    requests: list[Request] = []

    def capture(request: Request) -> Request:
        requests.append(request)
        return request

    monkeypatch.setattr(Entrez, "email", "offline@example.invalid")
    monkeypatch.setattr(Entrez, "api_key", None)
    monkeypatch.setattr(Entrez, "_open", capture)
    return requests


def _request_params(request: Request) -> dict[str, list[str]]:
    data = request.data
    if data is None:
        encoded = urlsplit(request.full_url).query
    else:
        assert isinstance(data, bytes)
        encoded = data.decode("utf-8")
    return parse_qs(encoded)


def test_requests_keep_the_service_key(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _capture_requests(monkeypatch)
    monkeypatch.setattr(Entrez, "api_key", "service-held-key")
    monkeypatch.setattr(entrez_rate_limit, "_await_slot", lambda: None)
    entrez_rate_limit.entrez_call(Entrez.esearch, db="pubmed", term="EGFR resistance")
    assert entrez_rate_limit._request_interval() == entrez_rate_limit._INTERVAL_WITH_API_KEY
    (request,) = requests
    assert "api_key=service-held-key" in request.full_url + str(request.data or "")


_TEST_INTERVAL = 0.05


class _Socket:
    def __init__(self, raw: bytes) -> None:
        self.stream = io.BytesIO(raw)

    def makefile(self, *_args: object, **_kwargs: object) -> io.BytesIO:
        return self.stream


class _FakeNcbi(BaseHandler):
    """Answers at the urllib opener, below Biopython's retry loop, so every
    physical send is observed."""

    handler_order = 100

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = statuses or []
        self.sent: list[float] = []
        self.guard = threading.Lock()
        self.release: threading.Event | None = None

    def https_open(self, req: Request) -> HTTPResponse:
        with self.guard:
            self.sent.append(time.monotonic())
            status = self.statuses.pop(0) if self.statuses else 200
        if self.release is not None:
            self.release.wait(timeout=5)
        body = (
            b'<?xml version="1.0" encoding="UTF-8" ?>\n'
            b'<!DOCTYPE eSearchResult PUBLIC "-//NLM//DTD esearch 20060628//EN"'
            b' "https://eutils.ncbi.nlm.nih.gov/eutils/dtd/20060628/esearch.dtd">\n'
            b"<eSearchResult><Count>0</Count><IdList></IdList></eSearchResult>"
        )
        head = f"HTTP/1.1 {status} {responses[status]}\r\nContent-Type: text/xml\r\n"
        raw = f"{head}Content-Length: {len(body)}\r\n\r\n".encode() + body
        response = HTTPResponse(cast(socket.socket, _Socket(raw)), method=req.get_method())
        response.begin()
        response.url = req.full_url
        return response


@pytest.fixture
def ncbi(monkeypatch: pytest.MonkeyPatch) -> _FakeNcbi:
    handler = _FakeNcbi()
    monkeypatch.setattr(urllib.request, "_opener", build_opener(handler))
    monkeypatch.setattr(entrez_rate_limit, "_request_interval", lambda: _TEST_INTERVAL)
    monkeypatch.setattr(entrez_rate_limit, "_next_slot", 0.0)
    monkeypatch.setattr(Entrez, "email", "offline@example.invalid")
    return handler


def _send() -> None:
    Entrez.urlopen(Request("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi")).close()


def test_biopython_and_its_parser_send_through_the_gate() -> None:
    assert Entrez.urlopen is Parser.urlopen
    assert Entrez.urlopen is not urllib.request.urlopen


class TestEntrezRateLimit:
    def test_concurrent_sends_do_not_burst(self, ncbi: _FakeNcbi) -> None:
        """Biopython's unlocked previous-request timestamp lets concurrent
        threads burst together."""
        _send()
        threads = [threading.Thread(target=_send) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Measure the whole span: descheduling can squeeze an adjacent gap but
        # cannot fake a burst-free span.
        issued = ncbi.sent[1:]
        assert len(issued) == 6
        span = max(issued) - min(issued)
        assert span >= 5 * _TEST_INTERVAL * 0.9, span

    def test_a_retried_429_waits_for_its_own_slot(
        self, ncbi: _FakeNcbi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Biopython retries a 429 immediately and paces only the first try."""
        monkeypatch.setattr(Entrez, "max_tries", 3)
        ncbi.statuses = [429]

        entrez_rate_limit.read_entrez(
            entrez_rate_limit.entrez_call(Entrez.esearch, db="pubmed", term="EGFR")
        )

        assert len(ncbi.sent) == 2
        assert ncbi.sent[1] - ncbi.sent[0] >= _TEST_INTERVAL * 0.9

    def test_the_wait_does_not_hold_the_lock(self, ncbi: _FakeNcbi) -> None:
        """Pacing bounds departure times; holding a lock over I/O serializes
        independent responses."""
        ncbi.release = threading.Event()
        slow_thread = threading.Thread(target=_send)
        slow_thread.start()

        quick_done = threading.Event()

        def quick() -> None:
            _send()
            quick_done.set()

        threading.Thread(target=quick).start()
        try:
            deadline = time.monotonic() + 5
            while len(ncbi.sent) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(ncbi.sent) == 2, "second send waited on the first response"
        finally:
            ncbi.release.set()
        slow_thread.join()
        assert quick_done.wait(timeout=5)


_IDS = [str(value) for value in range(1_000_000, 1_000_201)]


@pytest.mark.parametrize(
    ("request_ids", "method"),
    [(["101", "202", "303"], "GET"), (_IDS, "POST")],
)
def test_efetch_sends_the_id_list_as_one_comma_separated_value(
    monkeypatch: pytest.MonkeyPatch,
    request_ids: list[str],
    method: str,
) -> None:
    requests = _capture_requests(monkeypatch)

    Entrez.efetch(db="pubmed", id=request_ids, retmode="xml")

    (request,) = requests
    assert request.get_method() == method
    assert _request_params(request)["id"] == [",".join(request_ids)]


@pytest.mark.parametrize(
    ("request_ids", "method"),
    [(["101", "202", "303"], "GET"), (_IDS, "POST")],
)
def test_elink_sends_each_id_as_a_separate_parameter(
    monkeypatch: pytest.MonkeyPatch, request_ids: list[str], method: str
) -> None:
    requests = _capture_requests(monkeypatch)

    Entrez.elink(dbfrom="pubmed", db="pmc", linkname="pubmed_pmc", id=request_ids)

    (request,) = requests
    params = _request_params(request)
    assert request.get_method() == method
    assert params["id"] == request_ids
    assert params["linkname"] == ["pubmed_pmc"]
