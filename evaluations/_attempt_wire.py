"""Physical HTTP attempts, answered and counted below every client.

Shared by the engine process and the MCP subprocess of `attempt_envelope`.
It imports only the standard library and httpx, because the MCP subprocess
runs in its own interpreter without the engine installed.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import urllib.request
from collections.abc import Awaitable, Callable
from html import escape
from http.client import HTTPResponse, responses
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx

# Every non-local hostname resolves here, so URL guards pass on public data.
PUBLIC_IP = "93.184.216.34"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

CATEGORIES = (
    "llm_completion",
    "count_tokens",
    "pubmed_ncbi",
    "openalex",
    "arxiv",
    "europe_pmc",
    "brave_tavily",
    "read_url",
    "citation_probe",
    "other",
)
LLM_CATEGORIES = frozenset({"llm_completion", "count_tokens"})


class AttemptCounter:
    """Counts at the moment of sending, so failed and cancelled attempts
    count too. Each change is written to `path` for the parent process."""

    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self.counts: dict[str, int] = dict.fromkeys(CATEGORIES, 0)
        self.hosts: dict[str, int] = {}

    def record(self, category: str, host: str) -> None:
        with self._lock:
            self.counts[category] += 1
            self.hosts[host] = self.hosts.get(host, 0) + 1
            if self._path is not None:
                _write_json(self._path, {"counts": self.counts, "hosts": self.hosts})


def _write_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as handle:
        json.dump(payload, handle)
    os.replace(handle.name, path)


def category(host: str, path: str) -> str:
    if host == "api.anthropic.com":
        return "count_tokens" if path.endswith("/count_tokens") else "llm_completion"
    if host.endswith(".openai.azure.com"):
        return "llm_completion"
    if host == "eutils.ncbi.nlm.nih.gov":
        return "pubmed_ncbi"
    if host == "api.openalex.org":
        return "openalex"
    if host == "export.arxiv.org":
        return "arxiv"
    if host == "www.ebi.ac.uk":
        return "europe_pmc"
    if host in ("api.search.brave.com", "api.tavily.com"):
        return "brave_tavily"
    return "other"


def pin_dns() -> None:
    resolve = socket.getaddrinfo

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host is None or str(host) in LOCAL_HOSTS:
            return resolve(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 0))]

    socket.getaddrinfo = getaddrinfo


def _seed(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("\x00".join(parts).encode()).digest()[:4], "big")


def _topic(query: str) -> str:
    return " ".join(query.replace('"', " ").split())[:120] or "the topic"


def _abstract(topic: str, index: int) -> str:
    return (
        f"Study {index} reports measurements bearing on {topic}. Perturbing the pathway "
        "shifted the readout in a dose-dependent way across independent replicates."
    )


def _openalex(query: str, count: int) -> dict[str, Any]:
    topic = _topic(query)
    base = _seed("openalex", query)
    works = []
    for index in range(count):
        key = f"W{base + index}"
        words = _abstract(topic, index).split()
        works.append(
            {
                "id": f"https://openalex.org/{key}",
                "title": f"OpenAlex evidence on {topic} ({index})",
                "authorships": [{"author": {"display_name": "A. Author"}}],
                "publication_year": 2023,
                "abstract_inverted_index": {word: [i] for i, word in enumerate(words)},
                "primary_location": {"landing_page_url": f"https://doi.org/10.1000/{key}"},
                "doi": f"https://doi.org/10.1000/{key}",
                "cited_by_count": 12,
                "publication_date": "2023-05-01",
                "updated_date": "2024-01-01",
                "type": "article",
                "is_retracted": False,
            }
        )
    return {"results": works, "meta": {"next_cursor": None}}


def _europepmc(query: str, count: int) -> dict[str, Any]:
    topic = _topic(query)
    base = _seed("europepmc", query)
    preprint = "SRC:PPR" in query
    results = [
        {
            "id": f"{'PPR' if preprint else ''}{base + index}",
            "source": "PPR" if preprint else "MED",
            "pmid": None if preprint else str(base + index),
            "doi": f"10.1101/{base + index}" if preprint else f"10.2000/{base + index}",
            "title": f"Europe PMC evidence on {topic} ({index})",
            "abstractText": _abstract(topic, index),
            "pubYear": "2024",
            "authorString": "Author A, Author B.",
            "journalInfo": {"journal": {"title": "Journal of Offline Results"}},
            "citedByCount": 3,
        }
        for index in range(count)
    ]
    return {"resultList": {"result": results}}


def _arxiv(query: str, count: int) -> str:
    topic = escape(_topic(query))
    base = _seed("arxiv", query)
    entries = "".join(
        f"<entry><id>http://arxiv.org/abs/2401.{(base + index) % 100000:05d}v1</id>"
        f"<published>2024-01-0{index % 9 + 1}T00:00:00Z</published>"
        f"<title>arXiv evidence on {topic} ({index})</title>"
        f"<summary>{escape(_abstract(topic, index))}</summary>"
        "<author><name>A. Author</name></author></entry>"
        for index in range(count)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'


def _brave(query: str, count: int) -> dict[str, Any]:
    topic = _topic(query)
    base = _seed("brave", query)
    return {
        "web": {
            "results": [
                {
                    "title": f"Web page on {topic} ({index})",
                    "url": f"https://web.example/{base + index}",
                    "description": _abstract(topic, index),
                    "page_age": "2024-02-01",
                }
                for index in range(count)
            ]
        }
    }


def _tavily(query: str, count: int) -> dict[str, Any]:
    topic = _topic(query)
    base = _seed("tavily", query)
    return {
        "results": [
            {
                "title": f"Web page on {topic} ({index})",
                "url": f"https://web.example/{base + index}",
                "content": _abstract(topic, index),
                "score": 0.5,
            }
            for index in range(count)
        ]
    }


def _page(url: str) -> str:
    return (
        f"<html><head><title>Readable page {escape(url)}</title></head><body><article>"
        f"<h1>Findings</h1><p>{escape(_abstract(url, 1))}</p>"
        "<p>The authors discuss mechanisms, limitations and follow-up experiments.</p>"
        "</article></body></html>"
    )


def _int_param(params: dict[str, list[str]], *names: str, default: int) -> int:
    for name in names:
        if name in params:
            try:
                return max(0, int(params[name][0]))
            except ValueError:
                return default
    return default


def source_response(request: httpx.Request, kind: str) -> httpx.Response:
    params = parse_qs(request.url.query.decode())
    if request.method == "POST" and request.content:
        try:
            body = json.loads(request.content)
        except ValueError:
            body = {}
        params.update({key: [str(value)] for key, value in body.items()})
    query = (params.get("search") or params.get("query") or params.get("q") or [""])[0]
    count = min(_int_param(params, "per_page", "pageSize", "max_results", "count", default=5), 25)
    if kind == "openalex":
        return httpx.Response(200, json=_openalex(query, count))
    if kind == "europe_pmc":
        return httpx.Response(200, json=_europepmc(query, count))
    if kind == "arxiv":
        text = _arxiv(params.get("search_query", [query])[0], count)
        return httpx.Response(200, text=text, headers={"content-type": "application/atom+xml"})
    if kind == "brave_tavily":
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json=_tavily(query, count))
        return httpx.Response(200, json=_brave(query, count))
    if kind in ("read_url", "citation_probe"):
        source = str(request.extensions.get("source_url") or request.url)
        return httpx.Response(200, text=_page(source), headers={"content-type": "text/html"})
    return httpx.Response(404, json={"error": "not served offline"})


Responder = Callable[[httpx.Request, str], httpx.Response]


def _classify(counter: AttemptCounter, request: httpx.Request, pinned_kind: str) -> str | None:
    host = request.url.host
    if host in LOCAL_HOSTS:
        return None
    # A pinned fetch dials the IP; its validated hostname (the source metadata,
    # or the Host header on older code) names the source.
    pinned = "source_host" in request.extensions or host == PUBLIC_IP
    named = str(request.extensions.get("source_host") or request.headers.get("host") or host)
    kind = pinned_kind if pinned else category(named, request.url.path)
    counter.record(kind, named)
    return kind


def httpx_transport_handler(
    counter: AttemptCounter, respond: Responder
) -> Callable[[Any, httpx.Request], Awaitable[httpx.Response]]:
    """For transports that only ever leave the machine (LiteLLM's aiohttp)."""

    async def handle_async_request(_transport: Any, request: httpx.Request) -> httpx.Response:
        kind = _classify(counter, request, "read_url")
        if kind is None:
            raise RuntimeError("local request on an outbound-only transport")
        await request.aread()
        return respond(request, kind)

    return handle_async_request


def install_httpx(
    counter: AttemptCounter, respond: Responder, *, pinned_kind: str = "read_url"
) -> None:
    """Patch the transports that would open sockets; clients, retries and
    redirects above them run unchanged. Local traffic (engine to MCP) passes
    through uncounted."""
    async_send = httpx.AsyncHTTPTransport.handle_async_request
    sync_send = httpx.HTTPTransport.handle_request

    async def handle_async_request(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        kind = _classify(counter, request, pinned_kind)
        if kind is None:
            return await async_send(self, request)
        await request.aread()
        return respond(request, kind)

    def handle_request(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        kind = _classify(counter, request, pinned_kind)
        if kind is None:
            return sync_send(self, request)
        request.read()
        return respond(request, kind)

    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request  # type: ignore[method-assign]
    httpx.HTTPTransport.handle_request = handle_request  # type: ignore[method-assign]


_DTD_ESEARCH = (
    '<!DOCTYPE eSearchResult PUBLIC "-//NLM//DTD esearch 20060628//EN" '
    '"https://eutils.ncbi.nlm.nih.gov/eutils/dtd/20060628/esearch.dtd">'
)
_DTD_PUBMED = (
    '<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN" '
    '"https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">'
)
_DTD_ELINK = (
    '<!DOCTYPE eLinkResult PUBLIC "-//NLM//DTD elink 20101123//EN" '
    '"https://eutils.ncbi.nlm.nih.gov/eutils/dtd/20101123/elink.dtd">'
)
_XML = '<?xml version="1.0" encoding="UTF-8" ?>\n'
_DTD_PMC = (
    '<!DOCTYPE pmc-articleset PUBLIC "-//NLM//DTD ARTICLE SET 2.0//EN" '
    '"https://dtd.nlm.nih.gov/ncbi/pmc/articleset/nlm-articleset-2.0.dtd">'
)


def _ids(params: dict[str, list[str]]) -> list[str]:
    return [paper for value in params.get("id", []) for paper in value.split(",") if paper]


def _pubmed_article(pmid: str) -> str:
    topic = f"record {pmid}"
    return (
        '<PubmedArticle><MedlineCitation Status="MEDLINE" Owner="NLM">'
        f'<PMID Version="1">{pmid}</PMID>'
        "<DateRevised><Year>2024</Year><Month>01</Month><Day>02</Day></DateRevised>"
        '<Article PubModel="Print"><Journal><JournalIssue CitedMedium="Internet">'
        "<PubDate><Year>2023</Year></PubDate></JournalIssue>"
        "<Title>Journal of Offline Results</Title></Journal>"
        f"<ArticleTitle>PubMed evidence, {topic}</ArticleTitle>"
        f"<Abstract><AbstractText>{escape(_abstract(topic, int(pmid) % 7))}</AbstractText>"
        '</Abstract><AuthorList CompleteYN="Y"><Author ValidYN="Y"><LastName>Author</LastName>'
        "<ForeName>Ada</ForeName></Author></AuthorList><PublicationTypeList>"
        '<PublicationType UI="D016428">Journal Article</PublicationType>'
        "</PublicationTypeList></Article></MedlineCitation><PubmedData><ArticleIdList>"
        f'<ArticleId IdType="pubmed">{pmid}</ArticleId>'
        f'<ArticleId IdType="doi">10.3000/{pmid}</ArticleId>'
        "</ArticleIdList></PubmedData></PubmedArticle>"
    )


def _pmc_article(pmc_id: str) -> str:
    # Real PMC answers carry one article per ID, each naming its own PMCID.
    return (
        '<article article-type="research-article"><front><article-meta>'
        f'<article-id pub-id-type="pmcid">PMC{pmc_id}</article-id>'
        "</article-meta></front><body><sec><title>Results</title>"
        f"<p>Full text of open-access article {pmc_id}.</p></sec></body></article>"
    )


def _eutils_body(path: str, params: dict[str, list[str]]) -> tuple[int, bytes]:
    if path.endswith("esearch.fcgi"):
        term = params.get("term", [""])[0]
        retmax = _int_param(params, "retmax", default=20)
        base = 30_000_000 + _seed("pubmed", term) % 1_000_000
        ids = "".join(f"<Id>{base + index}</Id>" for index in range(retmax))
        body = (
            f"{_XML}{_DTD_ESEARCH}<eSearchResult><Count>{retmax}</Count>"
            f"<RetMax>{retmax}</RetMax><RetStart>0</RetStart><IdList>{ids}</IdList>"
            "</eSearchResult>"
        )
        return 200, body.encode()
    if path.endswith("efetch.fcgi") and params.get("db", ["pubmed"])[0] == "pmc":
        articles = "".join(_pmc_article(pmc_id) for pmc_id in _ids(params))
        return 200, f"{_XML}{_DTD_PMC}<pmc-articleset>{articles}</pmc-articleset>".encode()
    if path.endswith("efetch.fcgi"):
        articles = "".join(_pubmed_article(pmid) for pmid in _ids(params))
        return 200, f"{_XML}{_DTD_PUBMED}<PubmedArticleSet>{articles}</PubmedArticleSet>".encode()
    if path.endswith("elink.fcgi"):
        sets = "".join(
            "<LinkSet><DbFrom>pubmed</DbFrom>"
            f"<IdList><Id>{pmid}</Id></IdList>"
            + (
                "<LinkSetDb><DbTo>pmc</DbTo><LinkName>pubmed_pmc</LinkName>"
                f"<Link><Id>{int(pmid) + 5_000_000}</Id></Link></LinkSetDb>"
                if int(pmid) % 2 == 0
                else ""
            )
            + "</LinkSet>"
            for pmid in _ids(params)
        )
        return 200, f"{_XML}{_DTD_ELINK}<eLinkResult>{sets}</eLinkResult>".encode()
    return 404, b"not served offline"


class _Socket:
    def __init__(self, raw: bytes) -> None:
        self.stream = io.BytesIO(raw)

    def makefile(self, *_args: object, **_kwargs: object) -> io.BytesIO:
        return self.stream


class FakeEutils(urllib.request.BaseHandler):
    """Biopython sends through urllib; this answers below its retry loop."""

    handler_order = 100

    def __init__(self, counter: AttemptCounter) -> None:
        self.counter = counter

    def https_open(self, req: urllib.request.Request) -> HTTPResponse:
        split = urlsplit(req.full_url)
        host = split.hostname or ""
        data = req.data if isinstance(req.data, bytes) else b""
        params = parse_qs(split.query)
        params.update(parse_qs(data.decode()))
        endpoint = split.path.rsplit("/", 1)[-1].removesuffix(".fcgi")
        database = params.get("db", [""])[0]
        self.counter.record(category(host, split.path), f"{host}/{endpoint}:{database}")
        status, body = (
            _eutils_body(split.path, params) if host == "eutils.ncbi.nlm.nih.gov" else (404, b"")
        )
        head = f"HTTP/1.1 {status} {responses[status]}\r\nContent-Type: text/xml\r\n"
        raw = f"{head}Content-Length: {len(body)}\r\n\r\n".encode() + body
        response = HTTPResponse(cast(socket.socket, _Socket(raw)), method=req.get_method())
        response.begin()
        response.url = req.full_url
        return response


def install_urllib(counter: AttemptCounter) -> None:
    urllib.request.install_opener(urllib.request.build_opener(FakeEutils(counter)))
