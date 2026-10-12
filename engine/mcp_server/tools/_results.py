from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
from typing import Any, ParamSpec

import httpx

_P = ParamSpec("_P")


def failed(reason: Exception | str) -> dict[str, Any]:
    if isinstance(reason, Exception):
        while isinstance(reason.__cause__, Exception):
            reason = reason.__cause__
        if isinstance(reason, httpx.HTTPStatusError):
            detail = f"HTTP {reason.response.status_code}"
        elif isinstance(reason, (TimeoutError, httpx.TimeoutException)):
            detail = "timeout"
        elif isinstance(reason, httpx.HTTPError):
            detail = "network_error"
        else:
            detail = "invalid_response"
    else:
        detail = reason[:160]
    return {"status": "failed", "records": [], "error": detail}


def ok(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "ok", "records": records}


def keyed_records(records: dict[str, Any]) -> dict[str, Any]:
    return ok([{"source_id": key, **record} for key, record in records.items()])


def non_raising(
    tool: Callable[_P, Awaitable[dict[str, Any]]],
) -> Callable[_P, Coroutine[Any, Any, dict[str, Any]]]:
    # Record parsers and input validation can fail outside the HTTP try block.
    @wraps(tool)
    async def run(*args: _P.args, **kwargs: _P.kwargs) -> dict[str, Any]:
        try:
            return await tool(*args, **kwargs)
        except Exception as exc:
            return failed(exc)

    return run
