"""What a failed provider call logs, so operators can tell why it failed.

Production logs drop free-form text from this package, because an exception
can quote research content. The fields built here are the exception: the
provider's HTTP status, its error code and the short ``error.message`` it
returned; the log projection scrubs and shortens it. Only the message
inside a provider's JSON error envelope is kept, never its metadata, which
can echo flagged input.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# The error object of a JSON envelope inside an SDK message, e.g. OpenRouter's
# '{"error":{"message":"Rate limit exceeded: free-models-per-day","code":429}}'
# or Anthropic's '{"type":"error","error":{"type":"...","message":"..."}}'.
_ENVELOPE = re.compile(r"\"error\"\s*:\s*(?=\{)")
_DECODER = json.JSONDecoder()


def _status(error: BaseException) -> int | None:
    for source in (error, getattr(error, "response", None)):
        status = getattr(source, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _envelope(error: BaseException) -> dict[str, Any] | None:
    for text in (getattr(getattr(error, "response", None), "text", None), str(error)):
        if not isinstance(text, str):
            continue
        for match in _ENVELOPE.finditer(text):
            try:
                body, _ = _DECODER.raw_decode(text, match.end())
            except ValueError:
                continue
            if isinstance(body, dict):
                return body
    return None


def provider_error_fields(error: BaseException) -> dict[str, str | int | bool]:
    """Diagnosable, non-private facts about a failed provider call."""
    fields: dict[str, str | int | bool] = {"error_type": type(error).__name__}
    status = _status(error)
    body = _envelope(error)
    if body is not None:
        code = body.get("code", body.get("type"))
        if isinstance(code, (int, str)) and not isinstance(code, bool):
            fields["provider_code"] = code
            if status is None and isinstance(code, int):
                status = code
        message = body.get("message")
        if isinstance(message, str) and message.strip():
            fields["provider_message"] = message.strip()
    if status is not None:
        fields["status"] = status
    return fields


def diagnostic_extra(event: str, **fields: Any) -> dict[str, Any]:
    """``extra`` for a named diagnostic event with exported fields."""
    return {"diagnostic_event": event, "diagnostic_fields": fields}


def log_provider_failure(
    *,
    slot: str,
    model: str,
    error: BaseException,
    reason: str,
    blocked_until: float | None = None,
) -> None:
    """One line per provider failure that moves a call to another provider."""
    fields: dict[str, Any] = {
        "slot": slot,
        "model": model,
        "reason": reason,
        **provider_error_fields(error),
    }
    if blocked_until is not None:
        fields["blocked_until"] = round(blocked_until)
    logger.warning(
        "Provider %s failed (%s); trying the next provider",
        slot,
        reason,
        extra=diagnostic_extra("llm.provider_failure", **fields),
    )


def is_rate_limited(error: Exception) -> bool:
    """Provider SDKs can surface throttling as generic errors, so text
    complements the type.
    """
    if type(error).__name__ == "RateLimitError":
        return True
    text = str(error).lower()
    return "rate limit" in text or "ratelimit" in text


# Without headers, a burst estimate stays below the durable park threshold.
_PER_MINUTE_DEFAULT_SECONDS = 60.0


def _next_utc_midnight_epoch(now: float) -> float:
    current = datetime.fromtimestamp(now, tz=timezone.utc)
    tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _platform_reset_from_headers(error: Exception, now: float) -> tuple[float, str] | None:
    """LiteLLM exposes upstream headers through response, not a headers
    shortcut.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    retry_after = _parse_float(headers.get("retry-after"))
    if retry_after is not None:
        return now + retry_after, "retry_after_header"
    reset_ms = _parse_float(headers.get("x-ratelimit-reset"))
    if reset_ms is not None:
        return reset_ms / 1000.0, "x_ratelimit_reset_header"
    return None


def _platform_reset_from_message(error: Exception, now: float) -> tuple[float, str] | None:
    """Without headers, daily caps use UTC reset; short burst waits stay
    below the park threshold.
    """
    # OpenRouter hyphenates free-models-per-day; accept plain-English wording
    # too.
    text = str(error).lower()
    if "per day" in text or "per-day" in text:
        return _next_utc_midnight_epoch(now), "message_per_day"
    if "per minute" in text or "per-minute" in text or "free-models" in text:
        return now + _PER_MINUTE_DEFAULT_SECONDS, "message_per_minute"
    return None


def rate_limit_reset(error: Exception, now: float | None = None) -> tuple[float, str] | None:
    """When a rate limit lifts (epoch seconds) and how that was read, if the
    error says.
    """
    now = time.time() if now is None else now
    return _platform_reset_from_headers(error, now) or _platform_reset_from_message(error, now)


__all__ = [
    "diagnostic_extra",
    "is_rate_limited",
    "log_provider_failure",
    "provider_error_fields",
    "rate_limit_reset",
]
