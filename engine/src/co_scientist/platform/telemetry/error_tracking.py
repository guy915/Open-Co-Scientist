from __future__ import annotations

import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from co_scientist.core.byok_scope import redact_byok_text
from co_scientist.platform.telemetry.tracing import OTLP_HEADER_ENV_VARS, private_error_type

# Deployment credentials can surface in provider error text; any variable
# named like a secret is scrubbed wherever it appears in a report.
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|DSN)$")
_MIN_SECRET_LENGTH = 8
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _deployment_secrets() -> tuple[str, ...]:
    values = {
        value
        for name, value in os.environ.items()
        if _SECRET_NAME.search(name.upper()) and len(value) >= _MIN_SECRET_LENGTH
    }
    for name in OTLP_HEADER_ENV_VARS:
        raw = os.environ.get(name, "")
        values.update((raw, unquote(raw)))
        for header in raw.split(","):
            _, _, value = header.partition("=")
            values.update((value.strip(), unquote(value).strip()))
    return tuple(sorted((v for v in values if len(v) >= _MIN_SECRET_LENGTH), key=len, reverse=True))


def _scrub(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        value = redact_byok_text(value)
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(item, secrets) for item in value]
    return value


@lru_cache(maxsize=1)
def _source_files() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    return {str(path.resolve()): str(path.relative_to(root)) for path in root.rglob("*.py")}


def _frames(stacktrace: Any) -> list[dict[str, Any]]:
    if not isinstance(stacktrace, dict) or not isinstance(stacktrace.get("frames"), list):
        return []
    result = []
    for frame in stacktrace["frames"][-50:]:
        if not isinstance(frame, dict):
            continue
        path = frame.get("abs_path") or frame.get("filename")
        filename = _source_files().get(path) if isinstance(path, str) else None
        if filename is None:
            continue
        safe: dict[str, Any] = {"filename": "co_scientist/" + filename}
        for key in ("lineno", "colno"):
            value = frame.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**31:
                safe[key] = value
        result.append(safe)
    return result


def scrub_event(event: dict[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    # Known-key replacement cannot identify arbitrary research text. Build a
    # fresh event so SDK additions and nested diagnostic payloads stay private.
    result: dict[str, Any] = {
        "level": "error",
        "platform": "python",
        "message": "Application error",
    }
    event_id = event.get("event_id")
    if isinstance(event_id, str) and re.fullmatch(r"[a-fA-F0-9]{32}", event_id):
        result["event_id"] = event_id
    timestamp = event.get("timestamp")
    if (
        isinstance(timestamp, int | float)
        and not isinstance(timestamp, bool)
        and 0 <= timestamp <= 253402300800
        and math.isfinite(timestamp)
    ):
        result["timestamp"] = timestamp
    request = event.get("request")
    if (
        isinstance(request, dict)
        and isinstance(request.get("method"), str)
        and request["method"] in _HTTP_METHODS
    ):
        result["request"] = {"method": request["method"]}
    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, dict) else None
    if isinstance(values, list):
        result["exception"] = {
            "values": [
                {
                    "type": private_error_type(value.get("type")),
                    "value": "Error details withheld for privacy",
                    "stacktrace": {"frames": _frames(value.get("stacktrace"))},
                }
                for value in values[:5]
                if isinstance(value, dict)
            ]
        }
    scrubbed: dict[str, Any] = _scrub(result, secrets)
    return scrubbed


def init_error_tracking(dsn: str, environment: str) -> bool:
    """The SDK is imported only when a DSN is configured, so local runs, CI
    and forks neither pay its startup cost nor send anything.
    """
    if not dsn.strip():
        return False
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    secrets = _deployment_secrets()

    def before_send(event: Any, hint: Any) -> Any:
        hint["attachments"] = []
        return scrub_event(event, secrets)

    sentry_sdk.init(
        dsn=dsn.strip(),
        environment=environment.strip() or None,
        send_default_pii=False,
        max_breadcrumbs=0,
        before_breadcrumb=lambda breadcrumb, hint: None,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=None,
        before_send=before_send,
        before_send_transaction=lambda event, hint: None,
        # Auto-enabled AI and HTTP-client integrations attach prompts and
        # model output, and importing them adds 1.6 s to startup.
        auto_enabling_integrations=False,
        integrations=[StarletteIntegration(), FastApiIntegration()],
    )
    return True
