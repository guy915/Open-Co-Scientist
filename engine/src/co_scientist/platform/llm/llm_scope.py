from __future__ import annotations

import asyncio
import logging
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Coroutine,
    Iterator,
)
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from co_scientist.platform.llm.admission.call_budget import scoped_completion_budget
from co_scientist.platform.llm.admission.service import scoped_app_admission
from co_scientist.platform.llm.process_mode import offline_mode, production_routing_enabled
from co_scientist.platform.llm.routing import scoped_operator_routing
from co_scientist.platform.llm.telemetry import scoped_telemetry
from co_scientist.platform.telemetry.logging_setup import current_run_id

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

_active: ContextVar[bool] = ContextVar("app_call_scope_active", default=False)
# asyncio retains only weak task references; keep stream producers alive until
# cancellation and cleanup join them.
_stream_producers: set[asyncio.Task[None]] = set()


def in_app_call_scope() -> bool:
    return _active.get()


@contextmanager
def app_call_scope(surface: str) -> Iterator[None]:
    """Retries and tool rounds share one physical-call cap, separate from
    scientific run spend.
    """
    from co_scientist.core.config import settings

    token = _active.set(True)
    with (
        scoped_completion_budget(settings.app_llm_max_calls) as budget,
        scoped_app_admission(),
        scoped_telemetry(f"app.{surface}") as telemetry,
        scoped_operator_routing(
            current_run_id(), enabled=production_routing_enabled() and not offline_mode()
        ),
    ):
        try:
            yield
        finally:
            _active.reset(token)
            if budget.count:
                logger.info(
                    "App completion usage surface=%s calls=%s usage=%s",
                    surface,
                    budget.count,
                    telemetry.snapshot(),
                )


def budgeted(
    surface: str,
) -> Callable[[Callable[P, Coroutine[Any, Any, T]]], Callable[P, Coroutine[Any, Any, T]]]:

    def decorate(
        function: Callable[P, Coroutine[Any, Any, T]],
    ) -> Callable[P, Coroutine[Any, Any, T]]:
        @wraps(function)
        async def invoke(*args: P.args, **kwargs: P.kwargs) -> T:
            if in_app_call_scope():
                return await function(*args, **kwargs)
            with app_call_scope(surface):
                return await function(*args, **kwargs)

        return invoke

    return decorate


async def _produce(
    iterator: AsyncIterator[Any],
    queue: asyncio.Queue[tuple[str, Any]],
    surface: str,
) -> None:
    """The producer owns stream context across yields so consumer task
    changes cannot reset its credentials or budget.
    """
    with app_call_scope(surface):
        try:
            async for value in iterator:
                acknowledged = asyncio.get_running_loop().create_future()
                await queue.put(("item", (value, acknowledged)))
                await acknowledged
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            await queue.put(("error", error))
        else:
            await queue.put(("done", None))
        finally:
            await _close_iterator(iterator)


async def _close_iterator(iterator: AsyncIterator[Any]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


async def _scoped_stream(iterator: AsyncIterator[T], surface: str) -> AsyncGenerator[T, None]:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1)
    producer = asyncio.create_task(_produce(iterator, queue, surface))
    _stream_producers.add(producer)
    producer.add_done_callback(_stream_producers.discard)
    try:
        while True:
            kind, value = await queue.get()
            if kind == "done":
                return
            if kind == "error":
                raise value
            item, acknowledged = value
            yield item
            acknowledged.set_result(None)
    finally:
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer


def budgeted_stream(
    surface: str,
) -> Callable[[Callable[P, AsyncIterator[T]]], Callable[P, AsyncGenerator[T, None]]]:
    """Stream context belongs to its producer rather than its consumer."""

    def decorate(
        function: Callable[P, AsyncIterator[T]],
    ) -> Callable[P, AsyncGenerator[T, None]]:
        @wraps(function)
        def invoke(*args: P.args, **kwargs: P.kwargs) -> AsyncGenerator[T, None]:
            return _scoped_stream(function(*args, **kwargs), surface)

        return invoke

    return decorate


async def stream_chunks(
    response: Any,
    *,
    stall_seconds: float,
    total_seconds: float,
) -> AsyncIterator[Any]:
    """Bound silence separately from total duration: reasoning can make
    progress continuously without finishing the answer.
    """
    try:
        async for chunk in _timed_chunks(response, stall_seconds, total_seconds):
            yield chunk
    finally:
        close = getattr(response, "aclose", None)
        if close is not None:
            await close()


async def _timed_chunks(
    response: Any, stall_seconds: float, total_seconds: float
) -> AsyncIterator[Any]:
    deadline = asyncio.get_running_loop().time() + total_seconds
    iterator = response.__aiter__()
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"stream exceeded {total_seconds}s in total")
        try:
            chunk = await asyncio.wait_for(
                iterator.__anext__(), timeout=min(stall_seconds, remaining)
            )
        except StopAsyncIteration:
            return
        yield chunk


__all__ = ["stream_chunks"]
