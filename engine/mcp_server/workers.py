"""Keep admission held until blocking work has actually stopped."""

import asyncio
import functools
import threading
from collections.abc import Callable
from typing import TypeVar

_Result = TypeVar("_Result")

MAX_CONCURRENT_DNS_WORKERS = 8
_dns_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DNS_WORKERS)


async def run_dns_worker(fn: Callable[..., _Result], *args: object) -> _Result:
    # getaddrinfo has no portable timeout. Keep its own admission in the
    # actual thread so request deadlines can expire without admitting more DNS.
    if not _dns_slots.acquire(blocking=False):
        raise RuntimeError("DNS capacity is busy")
    slots = _dns_slots

    def work() -> _Result:
        try:
            return fn(*args)
        finally:
            slots.release()

    try:
        pending = asyncio.get_running_loop().run_in_executor(None, functools.partial(work))
    except BaseException:
        slots.release()
        raise
    # Retrieve abandoned failures without exporting provider or URL prose.
    pending.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(pending)


async def run_worker(fn: Callable[..., _Result], *args: object) -> _Result:
    pending = asyncio.create_task(asyncio.to_thread(fn, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(pending)
            break
        except asyncio.CancelledError:
            if pending.cancelled():
                raise
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
    if cancelled:
        raise asyncio.CancelledError()
    return result
