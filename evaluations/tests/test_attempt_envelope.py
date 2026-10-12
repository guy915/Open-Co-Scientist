from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from typing import Any
from urllib.request import Request

import httpx
import pytest

from evaluations import _attempt_wire as wire
from evaluations import attempt_envelope


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> wire.AttemptCounter:
    # Registered first so teardown restores the real transports.
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        httpx.AsyncHTTPTransport.handle_async_request,
    )
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", httpx.HTTPTransport.handle_request)
    return wire.AttemptCounter()


def _send(url: str, **kwargs: Any) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient() as client:
            request = client.build_request("GET", url, **kwargs)
            return await client.send(request)

    return asyncio.run(send())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.anthropic.com/v1/messages", "llm_completion"),
        ("https://api.anthropic.com/v1/messages/count_tokens", "count_tokens"),
        ("https://res.openai.azure.com/openai/v1/responses", "llm_completion"),
        ("https://api.openalex.org/works?search=x", "openalex"),
        ("https://export.arxiv.org/api/query", "arxiv"),
        ("https://www.ebi.ac.uk/europepmc/webservices/rest/search", "europe_pmc"),
        ("https://api.tavily.com/search", "brave_tavily"),
        ("https://rest.uniprot.org/uniprotkb/search", "other"),
    ],
)
def test_every_outbound_attempt_is_counted_by_category(
    counter: wire.AttemptCounter, url: str, expected: str
) -> None:
    wire.install_httpx(counter, lambda request, kind: httpx.Response(200, json={"kind": kind}))

    assert _send(url).json() == {"kind": expected}
    assert counter.counts[expected] == 1
    assert sum(counter.counts.values()) == 1


@pytest.mark.parametrize(
    "pinned",
    [
        pytest.param({"extensions": {"source_host": "doi.org"}}, id="source metadata"),
        pytest.param({"headers": {"Host": "doi.org"}}, id="older code: Host header"),
    ],
)
def test_a_pinned_fetch_counts_under_its_hostname(
    counter: wire.AttemptCounter, pinned: dict[str, Any]
) -> None:
    wire.install_httpx(counter, lambda *_: httpx.Response(200), pinned_kind="citation_probe")

    _send(f"https://{wire.PUBLIC_IP}/10.1/x", **pinned)

    assert counter.counts["citation_probe"] == 1
    assert counter.hosts == {"doi.org": 1}


def test_an_attempt_counts_even_when_its_answer_fails(counter: wire.AttemptCounter) -> None:
    def refuse(request: httpx.Request, kind: str) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    wire.install_httpx(counter, refuse)

    with pytest.raises(httpx.ConnectError):
        _send("https://api.openalex.org/works")
    assert counter.counts["openalex"] == 1


def _eutils(handler: wire.FakeEutils, url: str, data: bytes | None = None) -> ET.Element:
    response = handler.https_open(Request(url, data=data))
    body = response.read()
    return ET.fromstring(body.split(b">", 2)[2] if b"<!DOCTYPE" in body else body)


def test_fake_eutils_answers_each_requested_pmid_once_per_request() -> None:
    counter = wire.AttemptCounter()
    handler = wire.FakeEutils(counter)
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    search = _eutils(handler, f"{base}/esearch.fcgi?db=pubmed&term=crowding&retmax=3")
    ids = [node.text or "" for node in search.iter("Id")]
    articles = _eutils(handler, f"{base}/efetch.fcgi", f"db=pubmed&id={','.join(ids)}".encode())
    links = _eutils(handler, f"{base}/elink.fcgi?dbfrom=pubmed&db=pmc&id={ids[0]}&id={ids[1]}")

    assert len(ids) == 3
    assert [node.text for node in articles.iter("PMID")] == ids
    assert [link_set.findtext("IdList/Id") for link_set in links.iter("LinkSet")] == ids[:2]
    assert counter.counts["pubmed_ncbi"] == 3


def test_fake_eutils_answers_a_pmc_batch_with_one_article_per_id() -> None:
    counter = wire.AttemptCounter()
    handler = wire.FakeEutils(counter)
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=910,920"

    articles = _eutils(handler, url)

    assert [node.text for node in articles.iter("article-id")] == ["PMC910", "PMC920"]
    assert counter.counts["pubmed_ncbi"] == 1


def test_the_live_check_total_adds_the_fixed_waves_and_is_never_fitted_to_the_limit() -> None:
    arms = {
        name: {"llm_total": llm, "source_total": source, "final_report": True}
        for name, llm, source in (("anthropic", 100, 90), ("azure", 50, 60))
    }

    sequence = attempt_envelope._sequence(arms)

    extra = attempt_envelope.CACHE_WAVE_ATTEMPTS + attempt_envelope.JUDGING_ATTEMPTS
    assert sequence["llm_total"] == 150 + extra
    assert sequence["source_total"] == 150
    assert sequence["total"] == 300 + extra
    assert sequence["within_limit"] is False
