from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Bound logging-worker shutdown even if a callback ignores cancellation; cohort
# exit cannot wait indefinitely.
_STOP_TIMEOUT_SECONDS = 10.0


async def stop_litellm_logging_worker() -> None:
    """Only the owning event loop can cancel LiteLLM's logging worker;
    logging cleanup must never change the scientific outcome.
    """
    try:
        from litellm.litellm_core_utils.logging_worker import (
            GLOBAL_LOGGING_WORKER,
        )
    except Exception:
        logger.debug("litellm logging worker unavailable", exc_info=True)
        return

    # LiteLLM's private _bound_loop is its only record of the worker task's
    # owning loop.
    bound_loop = getattr(GLOBAL_LOGGING_WORKER, "_bound_loop", None)
    if bound_loop is not asyncio.get_running_loop():
        return

    try:
        await asyncio.wait_for(GLOBAL_LOGGING_WORKER.stop(), timeout=_STOP_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("Stopping the litellm logging worker failed", exc_info=True)


def run_in_scoped_loop(coro: Coroutine[Any, Any, _T]) -> _T:
    """Close the loop's own logging worker before discarding the temporary
    cohort loop.
    """

    async def _runner() -> _T:
        try:
            return await coro
        finally:
            await stop_litellm_logging_worker()

    return asyncio.run(_runner())
