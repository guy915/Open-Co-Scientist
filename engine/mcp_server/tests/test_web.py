import socket
from typing import Any

import httpx
import pytest
from mcp_server.tests._httpx import stub_failure, stub_responses, transport_responses
from mcp_server.tests.test_pdf_parser import _pdf
from mcp_server.tools import web_fetch, web_providers
from mcp_server.tools.web_fetch import (
    UrlNotFetchableError,
    check_fetchable,
    read_url,
)
from mcp_server.tools.web_providers import (
    _clear_credential_error,
    _record_credential_error,
    check_web_search_available,
    search_brave,
    search_tavily,
    search_web,
    web_search_credential_error,
)

_PUBLIC = {"example.com": "93.184.216.34"}


@pytest.fixture
def resolve_to(monkeypatch: pytest.MonkeyPatch) -> Any:

    def _apply(mapping: dict[str, str]) -> None:
        def resolve(host: str, *args: Any, **kwargs: Any) -> list[Any]:
            if host not in mapping:
                raise socket.gaierror(f"unknown host {host}")
            address = (mapping[host], 0)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", address)]

        monkeypatch.setattr(socket, "getaddrinfo", resolve)

    return _apply


def test_allows_public_https_url(resolve_to: Any) -> None:
    resolve_to(_PUBLIC)
    check_fetchable("https://example.com/article")


@pytest.mark.parametrize(
    ("url", "resolves", "reason"),
    [
        ("file:///etc/passwd", {}, "scheme"),
        ("http:///nowhere", {}, "no host"),
        (
            "http://localhost:8008/api/runs",
            {"localhost": "127.0.0.1"},
            "non-public",
        ),
        (
            "http://mcp.railway.internal:8888/mcp",
            {"mcp.railway.internal": "10.0.1.5"},
            "non-public",
        ),
        # Hostname text alone cannot detect public names resolving to
        # private addresses.
        (
            "http://127.0.0.1.nip.io/admin",
            {"127.0.0.1.nip.io": "127.0.0.1"},
            "non-public",
        ),
        ("http://2130706433/", {"2130706433": "127.0.0.1"}, "non-public"),
        (
            "http://metadata.example/",
            {"metadata.example": "169.254.169.254"},
            "non-public",
        ),
        ("http://169.254.169.254/latest/meta-data/", {}, "not allowed"),
        ("https://no-such-host.invalid/", {}, "does not resolve"),
    ],
)
def test_unsafe_urls_are_rejected(
    resolve_to: Any, url: str, resolves: dict[str, str], reason: str
) -> None:
    resolve_to(resolves)
    with pytest.raises(UrlNotFetchableError, match=reason):
        check_fetchable(url)


_PAGE = """
<html>
  <head><title>Trial readout</title><style>body{color:red}</style></head>
  <body>
    <nav><a href="/">Home</a><a href="/about">About</a></nav>
    <script>analytics()</script>
    <article>
      <h1>Phase 2 results</h1>
      <p>The primary endpoint was met.</p>
      <h2>Methods</h2>
      <ul><li>Randomized</li><li>Double blind</li></ul>
    </article>
    <footer>Copyright 2026</footer>
  </body>
</html>
"""


def _page(status: int, content: bytes, content_type: str | None) -> Any:
    return httpx.Response(
        status,
        content=content,
        headers={"content-type": content_type} if content_type else {},
        request=httpx.Request("GET", "https://example.com/a"),
    )


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(
        302,
        headers={"location": location},
        request=httpx.Request("GET", "https://example.com/a"),
    )


@pytest.mark.parametrize("url", ["http://localhost:8008/api/runs", "file:///x"])
async def test_read_url_reports_a_blocked_url_as_text(url: str) -> None:
    assert (await read_url(url))["status"] == "failed"


async def test_read_url_extracts_readable_text_without_page_furniture(
    monkeypatch: pytest.MonkeyPatch, resolve_to: Any
) -> None:
    resolve_to(_PUBLIC)
    stub_responses(monkeypatch, _page(200, _PAGE.encode(), "text/html; charset=utf-8"))

    text = (await read_url("https://example.com/a"))["records"][0]["content"]

    for kept in (
        "# Trial readout",
        "# Phase 2 results",
        "## Methods",
        "The primary endpoint was met.",
        "- Randomized",
    ):
        assert kept in text
    for noise in ("analytics()", "color:red", "About", "Copyright 2026"):
        assert noise not in text


async def test_read_url_extracts_an_ordinary_pdf_in_the_worker(
    monkeypatch: pytest.MonkeyPatch, resolve_to: Any
) -> None:
    resolve_to(_PUBLIC)
    transport_responses(
        monkeypatch,
        _page(200, _pdf(["isolated PDF control"]), "application/pdf"),
    )

    assert (
        "isolated PDF control"
        in (await read_url("https://example.com/paper.pdf"))["records"][0]["content"]
    )


async def test_read_url_rejects_a_stream_over_the_response_budget(
    monkeypatch: pytest.MonkeyPatch, resolve_to: Any
) -> None:
    resolve_to(_PUBLIC)
    monkeypatch.setattr(web_fetch, "_MAX_BYTES", 5)
    transport_responses(monkeypatch, _page(200, b"123456", "text/plain"))

    assert (await read_url("https://example.com/large"))["status"] == "failed"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<html><body><div>Bare content here</div></body></html>", "Bare"),
        (f"<html><body><p>{'x' * 60_000}</p></body></html>", "xxxx"),
    ],
)
async def test_read_url_handles_div_only_and_oversized_pages(
    monkeypatch: pytest.MonkeyPatch, resolve_to: Any, body: str, expected: str
) -> None:
    resolve_to(_PUBLIC)
    stub_responses(monkeypatch, _page(200, body.encode(), "text/html"))

    text = (await read_url("https://example.com/a"))["records"][0]["content"]

    assert expected in text
    assert len(text) <= 50_000


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_page(404, b"nope", None), "[error: HTTP 404"),
        (
            _page(200, b"\x00\x01", "image/png"),
            "[note: unsupported content type image/png",
        ),
    ],
)
async def test_read_url_reports_failures_and_unreadable_types_as_text(
    monkeypatch: pytest.MonkeyPatch,
    resolve_to: Any,
    response: httpx.Response,
    expected: str,
) -> None:
    resolve_to(_PUBLIC)
    stub_responses(monkeypatch, response)

    result = await read_url("https://example.com/a")
    if expected.startswith("[error:"):
        assert result["status"] == "failed"
    else:
        assert result["records"][0]["content"].startswith(expected)


async def test_read_url_screens_every_redirect_hop(
    monkeypatch: pytest.MonkeyPatch, resolve_to: Any
) -> None:
    resolve_to(_PUBLIC | {"internal.example": "10.0.0.5"})
    client = stub_responses(monkeypatch, _redirect("http://internal.example/admin"))

    result = await read_url("https://example.com/a")

    assert result["status"] == "failed"
    assert len(client.calls) == 1


async def test_read_url_follows_a_safe_redirect_and_cuts_off_loops(
    monkeypatch: pytest.MonkeyPatch, resolve_to: Any
) -> None:
    resolve_to(_PUBLIC)
    stub_responses(
        monkeypatch,
        _redirect("https://example.com/b"),
        _page(200, b"<p>Landed</p>", "text/html"),
    )
    assert "Landed" in (await read_url("https://example.com/a"))["records"][0]["content"]

    stub_responses(monkeypatch, *[_redirect("https://example.com/a")] * 5)
    result = await read_url("https://example.com/a")
    assert result["status"] == "failed"


_BRAVE_PAYLOAD: dict[str, Any] = {
    "web": {
        "results": [
            {
                "title": "<strong>GLP-1</strong> trial results",
                "url": "https://example.com/a",
                "description": "<strong>GLP-1</strong> met &amp; passed.",
                "page_age": "2026-05-01",
                "profile": {"name": "Example News"},
            },
            {
                "title": "Follow-up",
                "url": "https://example.org/b",
                # Strip markup before unescaping so encoded tags stay inert.
                "description": "&lt;script&gt;alert(1)&lt;/script&gt;",
            },
            {"title": "no url"},
            "not a dict",
        ]
    }
}


async def test_brave_results_normalize_into_clean_stable_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_responses(monkeypatch, *[_BRAVE_PAYLOAD] * 3)

    out = await search_brave("glp-1", 10, 0)
    again = await search_brave("glp-1", 10, 0)
    capped = await search_brave("glp-1", 1, 0)

    first, second = out["records"]
    assert first.pop("source_id") == again["records"][0]["source_id"]
    assert first == {
        "title": "GLP-1 trial results",
        "url": "https://example.com/a",
        "abstract": "GLP-1 met & passed.",
        "published_date": "2026-05-01",
        "site": "Example News",
        "source": "web",
    }
    assert second["abstract"] == "<script>alert(1)</script>"
    # Lineage and dedup depend on stable URL-based ids across runs.
    assert second["source_id"] == again["records"][1]["source_id"]
    assert len(capped["records"]) == 1


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> Any:
    def configure(brave: bool = False, tavily: bool = False, prefer: str | None = None) -> None:
        for name, present in (
            ("BRAVE_API_KEY", brave),
            ("TAVILY_API_KEY", tavily),
        ):
            if present:
                monkeypatch.setenv(name, "k")
            else:
                monkeypatch.delenv(name, raising=False)
        if prefer:
            monkeypatch.setenv("WEB_SEARCH_PROVIDER", prefer)
        else:
            monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)

    return configure


@pytest.fixture
def _clear_credential_state() -> Any:
    """Provider refusal records are process-wide."""
    _clear_credential_error()
    yield
    _clear_credential_error()


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid/search")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


@pytest.mark.usefixtures("_clear_credential_state")
class TestWebSearchProviders:
    @pytest.mark.parametrize(
        ("search", "key", "status", "provider"),
        [
            (search_brave, "BRAVE_API_KEY", 401, "brave"),
            (search_brave, "BRAVE_API_KEY", 402, "brave"),
            (search_brave, "BRAVE_API_KEY", 403, "brave"),
            (search_tavily, "TAVILY_API_KEY", 401, "tavily"),
            (search_tavily, "TAVILY_API_KEY", 432, "tavily"),
            (search_tavily, "TAVILY_API_KEY", 433, "tavily"),
        ],
    )
    async def test_a_rejected_key_is_recorded_not_swallowed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        search: Any,
        key: str,
        status: int,
        provider: str,
    ) -> None:
        monkeypatch.setenv(key, "k")
        stub_failure(monkeypatch, _status_error(status))

        assert (await search("anything", 5, 0))["status"] == "failed"

        recorded = web_search_credential_error()
        assert recorded is not None
        assert (recorded["provider"], recorded["status"]) == (provider, status)

    @pytest.mark.parametrize("failure", [_status_error(429), httpx.ConnectError("no route")])
    async def test_throttling_and_outages_say_nothing_about_the_credential(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        stub_failure(monkeypatch, failure)

        assert (await search_brave("anything", 5, 0))["status"] == "failed"
        assert web_search_credential_error() is None

    async def test_availability_follows_refusals_and_a_success_clears_them(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any
    ) -> None:
        keys()
        assert await check_web_search_available() is False

        keys(brave=True)
        assert await check_web_search_available() is True
        stub_failure(monkeypatch, _status_error(402))
        await search_brave("anything", 5, 0)
        assert await check_web_search_available() is False

        stub_responses(
            monkeypatch,
            {"web": {"results": [{"title": "t", "url": "https://e.com"}]}},
        )
        assert (await search_brave("anything", 5, 0))["status"] == "ok"
        assert web_search_credential_error() is None
        assert await check_web_search_available() is True

        keys(brave=True, tavily=True)
        _record_credential_error("brave", 402, "quota gone")
        assert await check_web_search_available() is True
        _record_credential_error("tavily", 432, "out of credits")
        assert await check_web_search_available() is False

    @pytest.mark.parametrize(
        ("brave_answer", "found", "asked"),
        [
            (_status_error(402), True, ["brave", "tavily"]),
            # An empty answer is an answer; only refusal justifies spending
            # another provider's quota.
            ({"web": {"results": []}}, False, ["brave"]),
        ],
    )
    async def test_search_web_falls_through_only_when_a_provider_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        keys: Any,
        brave_answer: Any,
        found: bool,
        asked: list[str],
    ) -> None:
        keys(brave=True, tavily=True)
        tavily_answer = {"results": [{"title": "found", "url": "https://e.com"}]}
        client = stub_responses(monkeypatch, brave_answer, tavily_answer)

        results = await search_web("anything")

        assert bool(results["records"]) is found
        assert ["brave" if "brave" in url else "tavily" for url, _ in client.calls] == asked


def _error_response(status: int, body: Any) -> httpx.Response:
    request = httpx.Request("GET", "https://example.invalid/search")
    if isinstance(body, str):
        return httpx.Response(status, text=body, request=request)
    return httpx.Response(status, json=body, request=request)


def _brave_error(status: int, code: str, reset: str | None = None) -> httpx.Response:
    # Shape of Brave's documented APIErrorResponse.
    response = _error_response(
        status,
        {"type": "ErrorResponse", "error": {"id": "e", "status": status, "code": code}},
    )
    if reset is not None:
        response.headers["X-RateLimit-Reset"] = reset
    return response


_TAVILY_FOUND = {"results": [{"title": "found", "url": "https://e.com"}]}
_BRAVE_FOUND = {"web": {"results": [{"title": "brave", "url": "https://b.com"}]}}


def _asked(client: Any) -> list[str]:
    return ["brave" if "brave" in url else "tavily" for url, _ in client.calls]


@pytest.mark.usefixtures("_clear_credential_state")
class TestQuotaAndRateLimits:
    @pytest.mark.parametrize("code", ["QUOTA_LIMITED", "USAGE_LIMIT_EXCEEDED", "CREDIT_EXHAUSTED"])
    async def test_an_exhausted_brave_allowance_is_a_refusal_that_moves_to_tavily(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any, code: str
    ) -> None:
        keys(brave=True, tavily=True)
        client = stub_responses(monkeypatch, _brave_error(429, code), _TAVILY_FOUND, _TAVILY_FOUND)

        assert (await search_web("first"))["records"]
        assert (await search_web("second"))["records"]

        assert _asked(client) == ["brave", "tavily", "tavily"]
        recorded = web_search_credential_error()
        assert recorded is not None
        assert (recorded["provider"], recorded["status"]) == ("brave", 429)

    @pytest.mark.parametrize(
        "throttle",
        [_brave_error(429, "RATE_LIMITED"), _error_response(429, "Too Many Requests")],
    )
    async def test_a_brave_rate_limit_moves_only_this_query_to_tavily(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any, throttle: httpx.Response
    ) -> None:
        keys(brave=True, tavily=True)
        client = stub_responses(monkeypatch, throttle, _TAVILY_FOUND, _BRAVE_FOUND)

        assert (await search_web("first"))["records"][0]["title"] == "found"
        assert (await search_web("second"))["records"][0]["title"] == "brave"

        assert _asked(client) == ["brave", "tavily", "brave"]
        assert web_search_credential_error() is None

    async def test_a_tavily_rate_limit_moves_this_query_to_brave(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any
    ) -> None:
        keys(brave=True, tavily=True, prefer="tavily")
        tavily_throttle = _error_response(
            429,
            {"detail": {"error": "Your request has been blocked due to excessive requests."}},
        )
        client = stub_responses(monkeypatch, tavily_throttle, _BRAVE_FOUND)

        assert (await search_web("q"))["records"][0]["title"] == "brave"
        assert _asked(client) == ["tavily", "brave"]
        assert web_search_credential_error() is None

    @pytest.mark.parametrize("status", [432, 433])
    async def test_an_exhausted_tavily_plan_is_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any, status: int
    ) -> None:
        keys(tavily=True)
        stub_responses(monkeypatch, _error_response(status, {"detail": {"error": "limit"}}))

        assert (await search_web("q"))["status"] == "failed"
        recorded = web_search_credential_error()
        assert recorded is not None
        assert (recorded["provider"], recorded["status"]) == ("tavily", status)

    async def test_when_every_provider_is_throttled_each_is_asked_once(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any
    ) -> None:
        keys(brave=True, tavily=True)
        client = stub_responses(
            monkeypatch, _brave_error(429, "RATE_LIMITED"), _error_response(429, "slow down")
        )

        results = await search_web("q")

        assert (results["status"], results["error"]) == ("failed", "HTTP 429")
        assert _asked(client) == ["brave", "tavily"]
        assert web_search_credential_error() is None

    async def test_brave_is_probed_again_once_every_provider_has_refused(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any
    ) -> None:
        keys(brave=True, tavily=True)
        client = stub_responses(
            monkeypatch,
            _brave_error(429, "QUOTA_LIMITED"),
            _error_response(432, {"detail": {"error": "limit"}}),
            _BRAVE_FOUND,
            _BRAVE_FOUND,
        )

        assert (await search_web("month ends"))["status"] == "failed"
        assert (await search_web("month resets"))["records"]
        assert (await search_web("next"))["records"]

        assert _asked(client) == ["brave", "tavily", "brave", "brave"]
        assert await check_web_search_available() is True


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(web_providers, "_clock", fake)
    return fake


_DAY = 24 * 60 * 60


@pytest.mark.usefixtures("_clear_credential_state")
class TestBraveQuotaReset:
    async def test_brave_is_asked_again_after_its_monthly_window_resets(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any, clock: FakeClock
    ) -> None:
        keys(brave=True, tavily=True)
        client = stub_responses(
            monkeypatch,
            _brave_error(429, "QUOTA_LIMITED", reset="1, 3600"),
            _TAVILY_FOUND,
            _TAVILY_FOUND,
            _BRAVE_FOUND,
            _BRAVE_FOUND,
        )

        assert (await search_web("month ends"))["records"][0]["title"] == "found"
        clock.now += 3599
        assert (await search_web("before reset"))["records"][0]["title"] == "found"
        assert await check_web_search_available() is True
        clock.now += 1
        assert (await search_web("after reset"))["records"][0]["title"] == "brave"
        assert (await search_web("next"))["records"][0]["title"] == "brave"

        assert _asked(client) == ["brave", "tavily", "tavily", "brave", "brave"]
        assert web_search_credential_error() is None

    async def test_a_repeated_refusal_after_the_reset_stores_the_new_reset(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any, clock: FakeClock
    ) -> None:
        keys(brave=True, tavily=True)
        client = stub_responses(
            monkeypatch,
            _brave_error(429, "USAGE_LIMIT_EXCEEDED", reset="1, 60"),
            _TAVILY_FOUND,
            _brave_error(429, "USAGE_LIMIT_EXCEEDED", reset="0, 7200"),
            _TAVILY_FOUND,
            _TAVILY_FOUND,
            _BRAVE_FOUND,
        )

        await search_web("first")
        clock.now += 60
        assert (await search_web("reset passed"))["records"][0]["title"] == "found"
        clock.now += 7199
        assert (await search_web("new reset pending"))["records"][0]["title"] == "found"
        clock.now += 1
        assert (await search_web("new reset passed"))["records"][0]["title"] == "brave"

        assert _asked(client) == ["brave", "tavily", "brave", "tavily", "tavily", "brave"]

    @pytest.mark.parametrize("reset", [None, "soon", "", "1, -5", "0, 0"])
    async def test_without_a_readable_reset_brave_is_asked_again_after_a_day(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any, clock: FakeClock, reset: str | None
    ) -> None:
        keys(brave=True, tavily=True)
        client = stub_responses(
            monkeypatch,
            _brave_error(429, "CREDIT_EXHAUSTED", reset=reset),
            _TAVILY_FOUND,
            _TAVILY_FOUND,
            _BRAVE_FOUND,
        )

        await search_web("first")
        clock.now += _DAY - 1
        assert (await search_web("same day"))["records"][0]["title"] == "found"
        clock.now += 1
        assert (await search_web("next day"))["records"][0]["title"] == "brave"

        assert _asked(client) == ["brave", "tavily", "tavily", "brave"]

    @pytest.mark.parametrize("body", [{}, {"error": {"code": "QUOTA_LIMITED"}}])
    async def test_a_rejected_key_stays_refused_after_any_reset(
        self, monkeypatch: pytest.MonkeyPatch, keys: Any, clock: FakeClock, body: Any
    ) -> None:
        keys(brave=True, tavily=True)
        refused = _error_response(402, body)
        refused.headers["X-RateLimit-Reset"] = "1, 60"
        client = stub_responses(monkeypatch, refused, _TAVILY_FOUND, _TAVILY_FOUND)

        await search_web("first")
        clock.now += 40 * _DAY
        assert (await search_web("much later"))["records"][0]["title"] == "found"

        assert _asked(client) == ["brave", "tavily", "tavily"]


@pytest.mark.usefixtures("_clear_credential_state")
@pytest.mark.parametrize(
    ("requested", "recency", "count", "freshness"),
    [
        (500, 5, "20", "pw"),
        (0, 0, "1", None),
        (8, 0, "8", None),
    ],
)
async def test_search_web_clamps_its_limits(
    monkeypatch: pytest.MonkeyPatch,
    keys: Any,
    requested: int,
    recency: int,
    count: str,
    freshness: str | None,
) -> None:
    keys(brave=True)
    client = stub_responses(monkeypatch, {})

    await search_web("q", max_results=requested, recency_days=recency)

    params = client.calls[0][1]
    assert params["count"] == count
    assert params.get("freshness") == freshness
