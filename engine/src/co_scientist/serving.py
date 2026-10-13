import os

from starlette.types import ASGIApp

from co_scientist.api.trusted_proxy import TrustedProxyMiddleware, trusted_networks


def create_app() -> ASGIApp:
    networks = trusted_networks(os.getenv("COSCIENTIST_TRUSTED_PROXY_CIDRS", ""))
    from co_scientist.main import app

    return TrustedProxyMiddleware(app, networks)
