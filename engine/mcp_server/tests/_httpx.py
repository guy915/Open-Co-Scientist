"""Import helpers as mcp_server.tests to avoid mypy resolving one file under
two module names."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp_server.server import mcp
from starlette.applications import Starlette

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.content


class StubResponse:
    def __init__(self, payload: Any, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> Any:
        return self._payload

    @property
    def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return str(self._payload)


class StubClient:
    def __init__(
        self,
        responses: list[Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._responses = list(responses) if responses else []
        self._error = error
        self.calls: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "StubClient":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def get(self, url: str, **kwargs: Any) -> StubResponse | httpx.Response:
        return self._serve(url, kwargs.get("params"))

    def build_request(self, method: str, url: Any, **kwargs: Any) -> httpx.Request:
        return httpx.Request(method, url, **kwargs)

    async def send(self, request: httpx.Request, **_: Any) -> httpx.Response:
        response = self._serve(str(request.url), None)
        if not isinstance(response, httpx.Response):
            raise AssertionError("streaming requests require an httpx.Response")
        if response.is_stream_consumed:
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=_AsyncBytes(response.content),
                request=request,
            )
        return response

    async def post(self, url: str, json: Any = None, **_: Any) -> StubResponse | httpx.Response:
        return self._serve(url, json)

    def _serve(self, url: str, payload: Any) -> StubResponse | httpx.Response:
        # Pagination mutates its parameter dict between calls.
        self.calls.append((url, dict(payload) if isinstance(payload, dict) else payload))
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise AssertionError(f"stub has no response queued for {url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, (StubResponse, httpx.Response)):
            return response
        return StubResponse(response)


def _install(monkeypatch: pytest.MonkeyPatch, client: StubClient) -> StubClient:
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: client)
    return client


def stub_responses(monkeypatch: pytest.MonkeyPatch, *payloads: Any) -> StubClient:
    return _install(monkeypatch, StubClient(responses=list(payloads)))


def stub_failure(monkeypatch: pytest.MonkeyPatch, error: Exception) -> StubClient:
    return _install(monkeypatch, StubClient(error=error))


def asgi_client_factory(app: Any) -> Callable[..., httpx.AsyncClient]:
    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("follow_redirects", None)
        return _REAL_ASYNC_CLIENT(
            **kwargs,
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    return factory


@asynccontextmanager
async def registered_tools() -> AsyncIterator[Any]:
    """A FastMCP client connected to the real registered server in-process."""
    mcp_app = mcp.http_app()
    app = Starlette(lifespan=mcp_app.lifespan)
    app.mount("/", mcp_app)
    transport = StreamableHttpTransport(
        "http://test/mcp", httpx_client_factory=asgi_client_factory(app)
    )
    async with app.router.lifespan_context(app), Client(transport) as client:
        yield client


def transport_responses(monkeypatch: pytest.MonkeyPatch, *responses: Any) -> list[httpx.Request]:
    queued = list(responses)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = queued.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=_AsyncBytes(response.content),
                request=request,
            )
        return httpx.Response(200, json=response)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )
    return requests
