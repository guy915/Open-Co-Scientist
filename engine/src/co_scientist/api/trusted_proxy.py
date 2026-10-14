from __future__ import annotations

import ipaddress

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from co_scientist.api.operator_access import has_admin_token

ProxyNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def trusted_networks(value: str) -> tuple[ProxyNetwork, ...]:
    if not value.strip():
        return ()
    try:
        networks = tuple(
            ipaddress.ip_network(item.strip(), strict=True) for item in value.split(",")
        )
        if any(network.prefixlen == 0 for network in networks):
            raise ValueError
    except ValueError:
        raise ValueError("invalid trusted proxy network configuration") from None
    return networks


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if "%" in value:
        raise ValueError
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _visitor_header(scope: Scope) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    values = [value for name, value in scope["headers"] if name == b"x-real-ip"]
    if len(values) != 1:
        raise ValueError
    return _address(values[0].decode("ascii").strip())


class TrustedProxyMiddleware:
    def __init__(self, app: ASGIApp, networks: tuple[ProxyNetwork, ...]) -> None:
        self.app = app
        self.networks = networks

    async def _dispatch(
        self, scope: Scope, receive: Receive, send: Send, peer: str | None, trusted: bool
    ) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and scope.get("path") == "/api/proxy-status"
        ):
            if not has_admin_token(Request(scope)):
                response = JSONResponse({"detail": "not found"}, status_code=404)
            else:
                visitor = scope.get("client")
                try:
                    header_ip = str(_visitor_header(scope))
                except (UnicodeDecodeError, ValueError):
                    header_ip = None
                response = JSONResponse(
                    {
                        "socket_peer": peer,
                        "visitor_ip": visitor[0] if visitor else None,
                        "header_ip": header_ip,
                        "trusted": trusted,
                    }
                )
            response.headers["Cache-Control"] = "no-store"
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        # Railway readiness has no visitor headers. This public endpoint exposes
        # no visitor or admission state, so preserve its raw peer and scheme.
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and scope.get("path") == "/health"
        ):
            await self.app(scope, receive, send)
            return
        peer = scope.get("client")
        try:
            address = _address(peer[0]) if peer else None
        except ValueError:
            address = None
        if address is None or not any(address in network for network in self.networks):
            await self._dispatch(
                scope, receive, send, str(address) if address is not None else None, False
            )
            return
        try:
            visitor = _visitor_header(scope)
        except (UnicodeDecodeError, ValueError):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "invalid proxy client address"}, status_code=400)(
                    scope, receive, send
                )
            return
        forwarded: Scope = dict(scope)
        forwarded["client"] = (str(visitor), 0)
        # Railway's edge terminates TLS and supplies X-Real-IP. Arbitrary XFF
        # chains are not an authority for either the visitor or the scheme.
        forwarded["scheme"] = "wss" if scope["type"] == "websocket" else "https"
        await self._dispatch(forwarded, receive, send, str(address), True)
