import json

import pytest
from fastapi import FastAPI
from mcp_server.auth_middleware import (
    MCP_AUTH_HEADER,
    MCP_LOCAL_AUTH_ENV,
    SharedSecretAuthMiddleware,
)
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


async def _root(request):  # type: ignore[no-untyped-def]
    return PlainTextResponse("status")


async def _tool_endpoint(request):  # type: ignore[no-untyped-def]
    return PlainTextResponse("tool result")


def _make_app(secret: str | None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", _root),
            Route("/mcp", _tool_endpoint, methods=["POST"]),
        ]
    )
    app.add_middleware(SharedSecretAuthMiddleware, secret=secret)
    return app


_SECRET = {MCP_AUTH_HEADER: "s3cret"}


@pytest.mark.parametrize(
    ("secret", "method", "path", "headers", "statuses", "body"),
    [
        (None, "POST", "/mcp", {}, {401}, None),
        ("", "POST", "/mcp", {}, {401}, None),
        ("   ", "POST", "/mcp", {}, {401}, None),
        (None, "GET", "/", {}, {200}, "status"),
        (None, "HEAD", "/", {}, {401}, None),
        (None, "POST", "/", {}, {401}, None),
        ("s3cret", "POST", "/", {}, {401}, None),
        (None, "GET", "/mcp", {}, {401}, None),
        ("s3cret", "POST", "/mcp", {}, {401}, None),
        ("s3cret", "POST", "/mcp", {MCP_AUTH_HEADER: "wrong"}, {401}, None),
        ("s3cret", "POST", "/mcp", _SECRET, {200}, "tool result"),
        # A host path must not exempt an unauthenticated tool call.
        ("s3cret", "POST", "/mcp", {"Host": "example.com/#"}, {400, 401}, None),
        ("s3cret", "POST", "/mcp", {"Host": "example.com/?"}, {400, 401}, None),
        ("s3cret", "GET", "/", {}, {200}, "status"),
    ],
)
def test_shared_secret_guards_tool_calls_(
    secret: str | None,
    method: str,
    path: str,
    headers: dict[str, str],
    statuses: set[int],
    body: str | None,
) -> None:
    response = TestClient(_make_app(secret)).request(method, path, headers=headers)

    assert response.status_code in statuses
    if body is not None:
        assert response.text == body


@pytest.mark.parametrize("flag", ["", "0", "true", "1"])
@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "192.0.2.1", "testclient"])
def test_unset_secret_local_opt_in_requires_an_actual_loopback_peer(
    monkeypatch: pytest.MonkeyPatch, flag: str, peer: str
) -> None:
    monkeypatch.setenv(MCP_LOCAL_AUTH_ENV, flag)
    client = TestClient(_make_app(None), client=(peer, 1234))
    response = client.post("/mcp", headers={"Host": "localhost", "X-Forwarded-For": "127.0.0.1"})
    expected = 200 if flag == "1" and peer in {"127.0.0.1", "::1"} else 401
    assert response.status_code == expected


def test_local_opt_in_never_bypasses_a_configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_LOCAL_AUTH_ENV, "1")
    client = TestClient(_make_app("s3cret"), client=("127.0.0.1", 1234))
    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers=_SECRET).status_code == 200


def test_mounted_mcp_http_preserves_auth_lifespan_and_stateless_requests() -> None:
    from mcp_server import server

    app = FastAPI(lifespan=server.mcp_http_app.lifespan)
    app.add_api_route("/", server.root)
    app.add_middleware(SharedSecretAuthMiddleware, secret="s3cret")
    app.mount("/", server.mcp_http_app)
    headers = {"Accept": "application/json, text/event-stream"}
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "1"},
        },
    }
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        for credential in ({}, {MCP_AUTH_HEADER: "wrong"}):
            rejected = client.post("/mcp/", json=initialize, headers=headers | credential)
            assert rejected.status_code == 401
        authorized = headers | _SECRET
        response = client.post("/mcp/", json=initialize, headers=authorized)
        assert response.status_code == 200
        assert "mcp-session-id" not in response.headers
        # Each request must work without session affinity or a carried session ID.
        for request_id in (2, 3):
            response = client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": request_id, "method": "tools/list"},
                headers=authorized | {"MCP-Protocol-Version": "2025-11-25"},
            )
            assert response.status_code == 200
            assert "mcp-session-id" not in response.headers
            data = next(
                line[6:] for line in response.text.splitlines() if line.startswith("data: ")
            )
            result = json.loads(data)
            assert result["id"] == request_id
            assert {tool["name"] for tool in result["result"]["tools"]} == {
                name for _, name in server._MCP_TOOLS
            }
