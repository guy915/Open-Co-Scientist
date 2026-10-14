from __future__ import annotations

import json

import pytest
import uvicorn
from co_scientist.api.trusted_proxy import TrustedProxyMiddleware, trusted_networks
from co_scientist.core.config import settings
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.main import app
from co_scientist.platform.db import connect, transaction
from co_scientist.platform.db.admission import claim_run, connecting_host
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send


async def _request(
    peer: str,
    headers: list[tuple[bytes, bytes]],
    *,
    networks: str = "100.64.0.0/24",
    scope_type: str = "http",
    path: str = "/",
    method: str = "GET",
) -> tuple[list[Scope], list[Message]]:
    scopes: list[Scope] = []
    messages: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        scopes.append(scope)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    middleware = TrustedProxyMiddleware(app, trusted_networks(networks))
    await middleware(
        {
            "type": scope_type,
            "client": (peer, 12345),
            "scheme": "http",
            "headers": headers,
            "method": method,
            "path": path,
        },
        receive,
        send,
    )
    return scopes, messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"x-real-ip", b"invalid")],
        [(b"x-real-ip", b"198.51.100.9"), (b"x-real-ip", b"198.51.100.10")],
        [(b"x-real-ip", b"198.51.100.9")],
    ],
)
async def test_readiness_preserves_raw_peer_without_parsing_visitor_headers(
    headers: list[tuple[bytes, bytes]],
) -> None:
    scopes, messages = await _request("100.64.0.1", headers, path="/health")
    assert len(scopes) == 1
    assert scopes[0]["client"] == ("100.64.0.1", 12345)
    assert scopes[0]["scheme"] == "http"
    assert scopes[0]["headers"] == headers
    assert messages == []


def test_actual_readiness_accepts_the_headerless_railway_probe() -> None:
    middleware = TrustedProxyMiddleware(app, trusted_networks("100.64.0.0/24"))
    client = TestClient(middleware, client=("100.64.0.1", 39947))
    response = client.get(
        "/health",
        headers={"Host": "healthcheck.railway.app", "User-Agent": "RailwayHealthCheck/1.0"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["model_name"] is None
    assert "100.64.0.1" not in response.text
    assert client.post("/api/runs").status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_type", "method", "path"),
    [
        ("http", "HEAD", "/health"),
        ("http", "POST", "/health"),
        ("http", "GET", "/health/"),
        ("http", "GET", "/api/proxy-status"),
        ("http", "POST", "/api/runs"),
        ("websocket", "GET", "/health"),
    ],
)
async def test_readiness_exception_does_not_relax_other_requests(
    scope_type: str, method: str, path: str
) -> None:
    scopes, messages = await _request(
        "100.64.0.1", [], scope_type=scope_type, method=method, path=path
    )
    assert scopes == []
    if scope_type == "websocket":
        assert messages == [{"type": "websocket.close", "code": 1008}]
    else:
        assert messages[0]["type"] == "http.response.start"
        assert messages[0]["status"] == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["203.0.113.2", "fd12::2", "127.0.0.1"])
async def test_untrusted_direct_and_private_peers_cannot_choose_their_bucket(peer: str) -> None:
    scopes, messages = await _request(
        peer,
        [
            (b"x-real-ip", b"198.51.100.9"),
            (b"x-forwarded-for", b"198.51.100.10"),
            (b"forwarded", b"for=198.51.100.11"),
        ],
    )
    assert scopes[0]["client"] == (peer, 12345)
    assert scopes[0]["scheme"] == "http"
    assert messages == []


@pytest.mark.asyncio
async def test_one_visitor_keeps_the_same_bucket_across_edge_peers() -> None:
    first, _ = await _request("100.64.0.2", [(b"x-real-ip", b"198.51.100.9")])
    second, _ = await _request("100.64.0.20", [(b"x-real-ip", b"198.51.100.9")])
    assert first[0]["client"] == second[0]["client"] == ("198.51.100.9", 0)
    assert first[0]["scheme"] == "https"


@pytest.mark.asyncio
async def test_other_forwarding_headers_do_not_override_the_edge_visitor() -> None:
    scopes, _ = await _request(
        "100.64.0.2",
        [
            (b"x-real-ip", b"198.51.100.9"),
            (b"x-forwarded-for", b"127.0.0.1, 198.51.100.10"),
            (b"forwarded", b"for=198.51.100.11;proto=http"),
            (b"x-forwarded-proto", b"http"),
        ],
    )
    assert scopes[0]["client"] == ("198.51.100.9", 0)
    assert scopes[0]["scheme"] == "https"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"x-real-ip", b"")],
        [(b"x-real-ip", b"198.51.100.9, 198.51.100.10")],
        [(b"x-real-ip", b"198.51.100.9"), (b"x-real-ip", b"198.51.100.10")],
        [(b"x-real-ip", b"visitor.example")],
        [(b"x-real-ip", b"fe80::1%eth0")],
        [(b"x-real-ip", b"\xff")],
    ],
)
async def test_invalid_or_ambiguous_edge_addresses_fail_closed(
    headers: list[tuple[bytes, bytes]],
) -> None:
    scopes, messages = await _request("100.64.0.2", headers)
    assert scopes == []
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 400
    assert messages[1]["type"] == "http.response.body"
    assert json.loads(messages[1]["body"]) == {"detail": "invalid proxy client address"}


@pytest.mark.asyncio
async def test_trusted_ipv6_is_canonical_and_mapped_ipv4_shares_its_bucket() -> None:
    v6, _ = await _request("100.64.0.2", [(b"x-real-ip", b"2001:0db8::0009")])
    mapped, _ = await _request("100.64.0.2", [(b"x-real-ip", b"::ffff:198.51.100.9")])
    assert v6[0]["client"] == ("2001:db8::9", 0)
    assert mapped[0]["client"] == ("198.51.100.9", 0)


@pytest.mark.asyncio
async def test_unconfigured_adapter_ignores_every_forwarding_header() -> None:
    scopes, _ = await _request("100.64.0.2", [(b"x-real-ip", b"198.51.100.9")], networks="")
    assert scopes[0]["client"] == ("100.64.0.2", 12345)


@pytest.mark.asyncio
async def test_uvicorn_does_not_rewrite_the_raw_peer_even_with_wildcard_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    middleware = TrustedProxyMiddleware(app, trusted_networks("100.64.0.0/24"))
    config = uvicorn.Config(middleware, proxy_headers=False, lifespan="off", log_config=None)
    config.load()

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        pass

    await config.loaded_app(
        {
            "type": "http",
            "client": ("203.0.113.2", 12345),
            "headers": [(b"x-forwarded-for", b"100.64.0.2"), (b"x-real-ip", b"127.0.0.1")],
            "scheme": "http",
        },
        receive,
        send,
    )
    assert seen[0]["client"] == ("203.0.113.2", 12345)


@pytest.mark.asyncio
async def test_distinct_visitors_are_separate_in_actual_durable_host_admission(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "free_runs_per_host_per_day", 1)
    first, _ = await _request("100.64.0.2", [(b"x-real-ip", b"198.51.100.9")])
    second, _ = await _request("100.64.0.2", [(b"x-real-ip", b"198.51.100.10")])
    for owner, scopes in (("first", first), ("second", second)):
        with transaction(isolated_db) as conn:
            peer = scopes[0]["client"]
            assert peer is not None
            claim_run(conn, owner, owner, connecting_host(peer[0]), free=True)
    with connect(isolated_db) as conn:
        rows = conn.execute("SELECT host FROM run_admissions").fetchall()
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    repeated, _ = await _request("100.64.0.20", [(b"x-real-ip", b"198.51.100.9")])
    peer = repeated[0]["client"]
    assert peer is not None
    with transaction(isolated_db) as conn, pytest.raises(ProviderAdmissionError):
        claim_run(conn, "repeat", "new-owner", connecting_host(peer[0]), free=True)


@pytest.mark.parametrize(
    "value", ["*", "0.0.0.0/0", "::/0", "100.64.0.2/24", "bad", "100.64.0.0/24,"]
)
def test_invalid_or_all_address_trust_configuration_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid trusted proxy network configuration$"):
        trusted_networks(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_proxy_diagnostics_require_the_operator_token(token: str | None) -> None:
    headers = [(b"x-real-ip", b"198.51.100.9")]
    if token is not None:
        headers.append((b"x-logs-token", token.encode()))
    scopes, messages = await _request("100.64.0.2", headers, path="/api/proxy-status")
    assert scopes == []
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 404
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
    assert messages[1]["type"] == "http.response.body"
    assert json.loads(messages[1]["body"]) == {"detail": "not found"}


@pytest.mark.asyncio
async def test_operator_proxy_diagnostics_expose_only_that_requests_address_metadata() -> None:
    _, messages = await _request(
        "100.64.0.2",
        [
            (b"x-real-ip", b"198.51.100.9"),
            (b"x-logs-token", b"synthetic-test-operator"),
            (b"x-forwarded-for", b"private-header-sentinel"),
        ],
        path="/api/proxy-status",
    )
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
    assert messages[1]["type"] == "http.response.body"
    assert json.loads(messages[1]["body"]) == {
        "socket_peer": "100.64.0.2",
        "visitor_ip": "198.51.100.9",
        "header_ip": "198.51.100.9",
        "trusted": True,
    }


@pytest.mark.asyncio
async def test_an_unset_operator_token_never_grants_proxy_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", None)
    _, messages = await _request(
        "100.64.0.2",
        [(b"x-real-ip", b"198.51.100.9"), (b"x-logs-token", b"synthetic-test-operator")],
        path="/api/proxy-status",
    )
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 404


@pytest.mark.asyncio
async def test_invalid_websocket_proxy_identity_is_closed_before_dispatch() -> None:
    scopes, messages = await _request("100.64.0.2", [], scope_type="websocket")
    assert scopes == []
    assert messages == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.asyncio
async def test_operator_can_qualify_header_overwrite_before_enabling_trust() -> None:
    _, messages = await _request(
        "100.64.0.2",
        [(b"x-real-ip", b"198.51.100.9"), (b"x-logs-token", b"synthetic-test-operator")],
        path="/api/proxy-status",
        networks="",
    )
    assert messages[1]["type"] == "http.response.body"
    assert json.loads(messages[1]["body"]) == {
        "socket_peer": "100.64.0.2",
        "visitor_ip": "100.64.0.2",
        "header_ip": "198.51.100.9",
        "trusted": False,
    }


@pytest.mark.asyncio
async def test_proxy_diagnostics_do_not_intercept_other_methods() -> None:
    scopes, messages = await _request(
        "100.64.0.2",
        [(b"x-real-ip", b"198.51.100.9"), (b"x-logs-token", b"synthetic-test-operator")],
        path="/api/proxy-status",
        method="POST",
    )
    assert scopes[0]["method"] == "POST"
    assert messages == []
