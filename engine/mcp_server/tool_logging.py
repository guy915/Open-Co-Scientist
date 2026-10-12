"""Empty results can mean a failed source or no matches; per-call logging
distinguishes them.
"""

import functools
import inspect
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

MAX_CONCURRENT_MCP_TOOLS = 8
_tool_slots = threading.BoundedSemaphore(MAX_CONCURRENT_MCP_TOOLS)


def _describe_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    return f"{len(args)} positional, {len(kwargs)} named"


def _describe_result(result: Any) -> str:
    parsed: Any = result
    size = None
    if isinstance(result, str):
        size = len(result)
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return f"{size} chars"
    if isinstance(parsed, dict) and parsed.get("status") == "failed":
        return "failed"
    if isinstance(parsed, dict) and isinstance(parsed.get("records"), list):
        parsed = parsed["records"]
    if isinstance(parsed, (dict, list)):
        count = len(parsed)
        detail = f"{count} item{'' if count == 1 else 's'}"
        if not count:
            detail = "empty"
        return detail if size is None else f"{detail}, {size} chars"
    return "scalar"


def _log_success(name: str, described: str, result: Any, started: float) -> None:
    logger.info(
        "tool %s(%s) -> %s in %dms",
        name,
        described,
        _describe_result(result),
        int((time.monotonic() - started) * 1000),
    )


def _log_failure(name: str, described: str, exc: BaseException, started: float) -> None:
    # Exception text and tracebacks can repeat complete queries and source text.
    kind = type(exc).__name__
    if kind not in {
        "ValueError",
        "TypeError",
        "RuntimeError",
        "TimeoutError",
        "ConnectionError",
        "OSError",
        "CancelledError",
    }:
        kind = "Exception"
    logger.error(
        "tool %s(%s) raised %s after %dms",
        name,
        described,
        kind,
        int((time.monotonic() - started) * 1000),
    )


def _wrap_async(fn: Callable[..., Any], name: str) -> Callable[..., Any]:

    @functools.wraps(fn)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _tool_slots.acquire(blocking=False):
            return {"status": "failed", "error": "Retrieval capacity is busy", "records": []}
        started = time.monotonic()
        described = _describe_args(args, kwargs)
        try:
            result = await fn(*args, **kwargs)
        except BaseException as exc:
            _log_failure(name, described, exc, started)
            raise
        finally:
            _tool_slots.release()
        _log_success(name, described, result, started)
        return result

    signature = inspect.signature(fn)
    async_wrapper.__signature__ = signature  # type: ignore[attr-defined]
    return async_wrapper


def _wrap_sync(fn: Callable[..., Any], name: str) -> Callable[..., Any]:

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _tool_slots.acquire(blocking=False):
            return {"status": "failed", "error": "Retrieval capacity is busy", "records": []}
        started = time.monotonic()
        described = _describe_args(args, kwargs)
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            _log_failure(name, described, exc, started)
            raise
        finally:
            _tool_slots.release()
        _log_success(name, described, result, started)
        return result

    signature = inspect.signature(fn)
    sync_wrapper.__signature__ = signature  # type: ignore[attr-defined]
    return sync_wrapper


def with_call_logging(fn: Callable[..., Any], name: str) -> Callable[..., Any]:
    """FastMCP reads wrapped docs and signatures; losing metadata erases tool
    descriptions and parameters.
    """
    if inspect.iscoroutinefunction(fn):
        return _wrap_async(fn, name)
    return _wrap_sync(fn, name)
