"""Tool access requires a secret; local opt-in never authorizes remote peers."""

import hmac
import ipaddress
import os

from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

MCP_SHARED_SECRET_ENV = "COSCIENTIST_MCP_SHARED_SECRET"
MCP_LOCAL_AUTH_ENV = "COSCIENTIST_MCP_ALLOW_UNAUTHENTICATED_LOCAL"

MCP_AUTH_HEADER = "X-MCP-Shared-Secret"


class SharedSecretAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, secret: str | None) -> None:
        super().__init__(app)
        self._secret = secret if secret and secret.strip() else None
        self._allow_local = os.environ.get(MCP_LOCAL_AUTH_ENV) == "1"

    def _local_development(self, request: Request) -> bool:
        if not self._allow_local or request.client is None:
            return False
        try:
            return ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            return False

    def _secret_matches(self, request: Request) -> bool:
        """Compare presented credentials without a prefix-dependent check."""
        presented = request.headers.get(MCP_AUTH_HEADER, "")
        return bool(self._secret) and hmac.compare_digest(
            presented.encode(), (self._secret or "").encode()
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Authorize the ASGI path the router receives. URL reconstruction
        # includes the caller's Host header and is not an auth boundary.
        if request.method == "GET" and request.scope["path"] == "/":
            return await call_next(request)
        authorized = (
            self._secret_matches(request) if self._secret else self._local_development(request)
        )
        if not authorized:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def resolve_shared_secret() -> str | None:
    return os.environ.get(MCP_SHARED_SECRET_ENV) or None
