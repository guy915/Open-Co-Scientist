import asyncio
from typing import Any

import httpx
import pytest
from mcp_server.tests._httpx import (
    StubResponse,
    registered_tools,
    stub_failure,
    stub_responses,
    transport_responses,
)
from mcp_server.tools.lit_review import (
    arxiv_search,
    europepmc_search,
    opencitations,
)
from mcp_server.tools.lit_review.openalex_search import (
    search_openalex,
)


@pytest.fixture(autouse=True)
def _reset_arxiv_request_pacer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arxiv_search, "_ARXIV_REQUEST_LOCK", asyncio.Lock(), raising=False)
    monkeypatch.setattr(arxiv_search, "_ARXIV_NEXT_REQUEST_AT", None, raising=False)


_FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.01234v2</id>
    <published>2024-01-15T10:00:00Z</published>
    <title>  A Model of
      Resistance Reversal  </title>
    <summary>An abstract that
      wraps across lines.</summary>
    <author><name>Jane Doe</name></author>
    <author><name>John Smith</name></author>
    {doi}
  </entry>
</feed>"""


def _feed(doi: str = "") -> str:
    doi_element = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    return _FEED_TEMPLATE.format(doi=doi_element)


@pytest.mark.parametrize("doi", ["10.1234/foo", None])
async def test_an_arxiv_feed_normalizes_to_records(
    monkeypatch: pytest.MonkeyPatch, doi: str | None
) -> None:
    transport_responses(monkeypatch, httpx.Response(200, text=_feed(doi=doi or "")))

    result = await arxiv_search.search_arxiv("resistance reversal")

    assert result["status"] == "ok"
    (record,) = result["records"]
    assert record["title"] == "A Model of Resistance Reversal"
    assert record["abstract"] == "An abstract that wraps across lines."
    assert record["year"] == 2024
    assert record["authors"] == ["Jane Doe", "John Smith"]
    assert record["doi"] == doi
    assert record["is_preprint"] is True
    assert record["url"] == "http://arxiv.org/abs/2401.01234v2"
    # Position-based ids let a later query overwrite unrelated results.
    assert record["source_id"] == "2401.01234"


@pytest.mark.parametrize(
    "entry_id", ["", "<id>http://arxiv.org/api/errors#incorrect_id_format</id>"]
)
async def test_an_arxiv_query_error_is_failed_not_zero_hits(
    monkeypatch: pytest.MonkeyPatch,
    entry_id: str,
) -> None:
    # Query errors can look like entries; admitting them invents empty-titled
    # papers.
    transport_responses(
        monkeypatch,
        httpx.Response(
            200,
            text=f'<feed xmlns="http://www.w3.org/2005/Atom"><entry>{entry_id}<title>error</title></entry></feed>',
        ),
    )

    result = await arxiv_search.search_arxiv("resistance reversal")

    assert result == {"status": "failed", "records": [], "error": "invalid_response"}


async def test_arxiv_retries_a_429_once_the_rate_limit_window_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    monkeypatch.setattr(arxiv_search, "_sleep", sleep, raising=False)
    monkeypatch.setattr(arxiv_search, "_monotonic", lambda: now, raising=False)
    requests = transport_responses(
        monkeypatch,
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, text=_feed()),
    )

    result = await arxiv_search.search_arxiv("resistance reversal")

    assert result["status"] == "ok"
    assert len(requests) == 2
    assert delays == [3.0]


async def test_arxiv_does_not_retry_past_the_bounded_retry_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    monkeypatch.setattr(arxiv_search, "_sleep", sleep, raising=False)
    monkeypatch.setattr(arxiv_search, "_monotonic", lambda: now, raising=False)
    requests = transport_responses(
        monkeypatch,
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(200, text=_feed()),
    )

    result = await arxiv_search.search_arxiv("resistance reversal")
    queued_result = await arxiv_search.search_arxiv("another query")

    assert result == {"status": "failed", "records": [], "error": "HTTP 429"}
    assert queued_result == {"status": "failed", "records": [], "error": "HTTP 429"}
    assert len(requests) == 1
    assert delays == []


async def test_concurrent_arxiv_searches_respect_a_long_rate_limit_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    monkeypatch.setattr(arxiv_search, "_sleep", sleep, raising=False)
    monkeypatch.setattr(arxiv_search, "_monotonic", lambda: now, raising=False)
    requests = transport_responses(
        monkeypatch,
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(200, text=_feed()),
    )

    results = await asyncio.gather(
        arxiv_search.search_arxiv("first query"),
        arxiv_search.search_arxiv("queued query"),
    )

    assert [result["error"] for result in results] == ["HTTP 429", "HTTP 429"]
    assert len(requests) == 1
    assert delays == []


async def test_concurrent_arxiv_searches_are_process_paced_and_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    starts: list[float] = []
    in_flight = 0
    peak = 0
    real_sleep = asyncio.sleep

    async def sleep(delay: float) -> None:
        nonlocal now
        now += delay
        await real_sleep(0)

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_: Any) -> bool:
            return False

        async def get(self, *_: Any, **__: Any) -> httpx.Response:
            nonlocal in_flight, peak
            starts.append(now)
            in_flight += 1
            peak = max(peak, in_flight)
            await real_sleep(0)
            in_flight -= 1
            return httpx.Response(200, text=_feed())

    monkeypatch.setattr(arxiv_search, "_sleep", sleep, raising=False)
    monkeypatch.setattr(arxiv_search, "_monotonic", lambda: now, raising=False)
    monkeypatch.setattr(arxiv_search, "make_client", lambda _timeout: Client())

    await asyncio.gather(
        arxiv_search.search_arxiv("resistance"),
        arxiv_search.search_arxiv("cancer"),
    )

    assert starts == [0.0, 3.0]
    assert peak == 1


async def test_cancelling_an_arxiv_retry_releases_the_process_pacer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    backoff_started = asyncio.Event()
    release_backoff = asyncio.Event()
    first_sleep = True

    async def sleep(delay: float) -> None:
        nonlocal first_sleep, now
        if first_sleep:
            first_sleep = False
            backoff_started.set()
            await release_backoff.wait()
        now += delay

    monkeypatch.setattr(arxiv_search, "_sleep", sleep, raising=False)
    monkeypatch.setattr(arxiv_search, "_monotonic", lambda: now, raising=False)
    requests = transport_responses(
        monkeypatch,
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, text=_feed()),
    )

    search = asyncio.create_task(arxiv_search.search_arxiv("first query"))
    await asyncio.wait_for(backoff_started.wait(), timeout=1)
    search.cancel()
    with pytest.raises(asyncio.CancelledError):
        await search

    result = await asyncio.wait_for(arxiv_search.search_arxiv("next query"), timeout=1)

    assert result["status"] == "ok"
    assert len(requests) == 2


@pytest.fixture
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(europepmc_search, "_TRANSPORT_RETRY_DELAYS_SECONDS", (0.0, 0.0))


def _payload(source: str = "MED", **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "id": "42387642",
        "source": source,
        "title": "PKMYT1 in Cancer",
        "abstractText": "PKMYT1 has emerged as a target.",
        "pubYear": "2026",
        "doi": "10.1002/gcc.70151",
        "authorString": "Li Y, Chen X.",
        "citedByCount": 3,
    }
    return {"resultList": {"result": [result | overrides]}}


@pytest.mark.usefixtures("_no_retry_wait")
class TestEuropepmcSearch:
    @pytest.mark.parametrize(("source", "preprint"), [("PPR", True), ("MED", False)])
    async def test_a_record_says_whether_it_was_peer_reviewed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        source: str,
        preprint: bool,
    ) -> None:
        stub_responses(monkeypatch, _payload(source=source))

        result = await europepmc_search.search_europepmc("PKMYT1")

        (record,) = result["records"]
        assert record["is_preprint"] is preprint
        assert record["source_id"] == f"{source}/42387642"
        assert record["doi"] == "10.1002/gcc.70151"
        assert record["url"] == "https://doi.org/10.1002/gcc.70151"
        assert record["cited_by_count"] == 3

    @pytest.mark.parametrize(
        ("tool", "source"),
        [
            (europepmc_search.search_europepmc, "Europe PMC"),
            (europepmc_search.search_preprints, "Preprints"),
            (europepmc_search.search_biorxiv, "bioRxiv"),
        ],
    )
    async def test_a_failed_request_is_distinct_from_an_empty_search(
        self, monkeypatch: pytest.MonkeyPatch, tool: Any, source: str
    ) -> None:
        stub_failure(monkeypatch, httpx.ConnectError("boom"))

        assert (await tool("PKMYT1")) == {
            "status": "failed",
            "records": [],
            "error": "network_error",
        }

    async def test_a_persistent_transport_failure_still_fails_the_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = stub_failure(monkeypatch, httpx.RemoteProtocolError("Server disconnected"))

        assert (await europepmc_search.search_europepmc("PKMYT1"))["status"] == "failed"

        assert len(client.calls) == 3


_SAMPLE: dict[str, Any] = {
    "results": [
        {
            "id": "https://openalex.org/W123",
            "title": "Ambient nitrogen fixation",
            "publication_year": 2023,
            "authorships": [
                {"author": {"display_name": "Ada Lovelace"}},
                {"author": {"display_name": "Alan Turing"}},
            ],
            "abstract_inverted_index": {
                "Nitrogen": [0],
                "fixation": [1],
                "matters": [2],
            },
            "primary_location": {"landing_page_url": "https://example/w123"},
            "doi": "https://doi.org/10.1/x",
        },
        {
            "id": "https://openalex.org/W456",
            "display_name": "Second work",
            "publication_year": 2020,
            "authorships": [],
            "abstract_inverted_index": None,
            "primary_location": {},
            "doi": "https://doi.org/10.2/y",
        },
    ]
}


async def test_openalex_failure_returns_failed_instead_of_looking_like_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_responses(monkeypatch, StubResponse(None, error=httpx.HTTPError("x")))

    assert (await search_openalex("q"))["status"] == "failed"


async def test_openalex_follows_cursor_pages_and_excludes_retractions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEY", "key")
    second = {
        "results": [{"id": "https://openalex.org/W789", "title": "Third work"}],
        "meta": {"next_cursor": None},
    }
    first = {**_SAMPLE, "meta": {"next_cursor": "cursor-2"}}
    client = stub_responses(monkeypatch, first, second)

    out = await search_openalex("nitrogen fixation", max_papers=3, recency_years=5)
    capped = stub_responses(monkeypatch, first)
    assert len((await search_openalex("nitrogen fixation", max_papers=1))["records"]) == 1

    assert {r["source_id"] for r in out["records"]} == {"W123", "W456", "W789"}
    assert [str(params["cursor"]) for _, params in client.calls] == [
        "*",
        "cursor-2",
    ]
    assert client.calls[0][1]["api_key"] == "key"
    assert "is_retracted:false" in client.calls[0][1]["filter"]
    assert "from_publication_date:" in client.calls[0][1]["filter"]
    assert len(capped.calls) == 1


_DOI = "10.1108/jd-12-2013-0166"


async def _no_wait_for_slot() -> None:
    """Fake HTTP responses must not wait for the process-wide real-network
    pacer."""


def _install_responses(
    monkeypatch: pytest.MonkeyPatch, responses: list[Any]
) -> list[httpx.Request]:
    requests = transport_responses(monkeypatch, *responses)
    monkeypatch.setattr(opencitations, "_wait_for_request_slot", _no_wait_for_slot)
    return requests


async def test_citation_edges_is_available_on_the_mcp_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_responses(
        monkeypatch,
        [
            [{"count": "1"}],
            [{"count": "1"}],
            [
                {
                    "oci": "1-2",
                    "citing": "omid:br/1 doi:10.1234/citing",
                    "cited": f"doi:{_DOI} omid:br/2",
                    "creation": "2025-01-01",
                }
            ],
            [
                {
                    "oci": "3-4",
                    "citing": f"doi:{_DOI} omid:br/3",
                    "cited": "doi:10.1234/referenced",
                }
            ],
        ],
    )
    tool_name = "get_opencitations_citation_edges"

    async with registered_tools() as client:
        tools = await client.list_tools()
        result = await client.call_tool(tool_name, {"doi": _DOI})

    assert tool_name in {tool.name for tool in tools}
    assert result.data["records"][0]["citations"]["edges"][0]["cited"] == [
        f"doi:{_DOI}",
        "omid:br/2",
    ]
    assert result.data["records"][0]["references"]["edges"][0]["citing"] == [
        f"doi:{_DOI}",
        "omid:br/3",
    ]
    assert result.data["records"][0]["source"] == "OpenCitations Index v2"
    assert result.data["records"][0]["citations"]["edge_request_url"] == str(requests[2].url)
    assert result.data["records"][0]["references"]["edge_request_url"] == str(requests[3].url)
    assert "do not establish" in result.data["records"][0]["interpretation_note"]
    assert len(requests) == 4


_ONE_EDGE = {"oci": "1-2", "citing": f"doi:{_DOI}", "cited": "doi:10.1111/x"}


@pytest.mark.parametrize(
    ("responses", "error", "request_count"),
    [
        pytest.param(
            [httpx.Response(200, content=b"x" * (opencitations._MAX_RESPONSE_BYTES + 1))],
            "1 MB limit",
            1,
            id="large response",
        ),
        pytest.param(
            [
                [{"count": "50"}],
                [{"count": "0"}],
                [{**_ONE_EDGE, "oci": f"{n}-2"} for n in range(1, 52)],
            ],
            "edge count exceeds",
            3,
            id="edge count grew past the limit",
        ),
        pytest.param(
            [httpx.Response(503, json={"error": "unavailable"})],
            r"HTTPStatusError.*503",
            1,
            id="upstream error is not reported as zero",
        ),
        pytest.param(
            [
                [{"count": "1"}],
                [{"count": "0"}],
                [{**_ONE_EDGE, "citing": f"doi:10.1234/{'x' * 1024}"}],
            ],
            "identifier text exceeds",
            3,
            id="identifier text is bounded",
        ),
    ],
)
async def test_unusable_upstream_answers_fail_instead_of_looking_complete(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any],
    error: str,
    request_count: int,
) -> None:
    requests = _install_responses(monkeypatch, responses)

    result = await opencitations.get_opencitations_citation_edges(_DOI)
    assert result["status"] == "failed"
    assert result["records"] == []

    assert len(requests) == request_count
