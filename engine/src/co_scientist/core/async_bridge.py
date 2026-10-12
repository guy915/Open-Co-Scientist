from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ParamSpec, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_P = ParamSpec("_P")

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _ensure_bridge_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever,
                name="app-async-bridge",
                daemon=True,
            ).start()
            _loop = loop
        return _loop


def propagate_context(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Capture at submission: one Context cannot be entered concurrently.
    Replay independent values and reset reused workers.
    """
    items = list(contextvars.copy_context().items())

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> _T:
        tokens = [(var, var.set(value)) for var, value in items]
        try:
            return fn(*args, **kwargs)
        finally:
            for var, token in reversed(tokens):
                var.reset(token)

    return _wrapped


def run_coroutine_sync(coro_factory: Callable[[], Awaitable[_T]]) -> _T:
    """A persistent bridge loop preserves LiteLLM's loop-bound clients;
    scoped values reset before later calls to prevent cross-run
    contamination.
    """
    loop = _ensure_bridge_loop()
    items = list(contextvars.copy_context().items())

    async def _runner() -> _T:
        tokens = [(var, var.set(value)) for var, value in items]
        try:
            return await coro_factory()
        finally:
            for var, token in reversed(tokens):
                var.reset(token)

    future = asyncio.run_coroutine_threadsafe(_runner(), loop)
    return future.result()


async def run_off_loop(call: Callable[[], _T]) -> _T:
    """A dedicated thread keeps the task loop free to renew leases;
    explicitly propagate budget and telemetry context.
    """
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as host:
        return await loop.run_in_executor(host, propagate_context(call))


# Run worker pools hold anyio's default threadpool for a run's lifetime, so
# request handlers get threads of their own.
_handler_threads = ThreadPoolExecutor(max_workers=4, thread_name_prefix="api-handler")


def off_loop(handler: Callable[_P, _T]) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """A blocked SQLite call (30 s busy timeout) then stalls one request,
    not every request and event stream.
    """

    @functools.wraps(handler)
    async def _endpoint(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        call = functools.partial(propagate_context(handler), *args, **kwargs)
        return await asyncio.get_running_loop().run_in_executor(_handler_threads, call)

    return _endpoint
