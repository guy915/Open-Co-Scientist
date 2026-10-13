"""One server span per request, named by the route template so paths with
ids aggregate; no query strings, headers or bodies.
"""

from __future__ import annotations

from opentelemetry import trace
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from co_scientist.platform.telemetry.tracing import current_span


class TracingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"]
        attributes = {"http.request.method": method}
        with current_span(f"HTTP {method}", attributes, trace.SpanKind.SERVER) as span:

            async def send_with_status(message: Message) -> None:
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                    span.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        span.set_status(trace.StatusCode.ERROR)
                await send(message)

            try:
                await self.app(scope, receive, send_with_status)
            finally:
                # The router records the matched route on the shared scope.
                template = getattr(scope.get("route"), "path", None)
                if isinstance(template, str):
                    span.set_attribute("http.route", template)
                    span.update_name(f"{method} {template}")
