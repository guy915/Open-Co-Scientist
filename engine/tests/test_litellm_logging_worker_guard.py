from __future__ import annotations

import asyncio
import gc
import logging
import threading
from typing import Any

import litellm
import pytest
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

import co_scientist.platform.llm.request.backend  # noqa: F401  (installs the guard)


async def _mock_call() -> None:
    await litellm.acompletion(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        mock_response="ok",
    )
    await asyncio.sleep(0.05)


def test_calls_from_two_event_loops_leave_no_orphaned_worker_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Production shape: the API loop and a run's worker-thread loop both call
    # the SDK. Before the guard, the second loop rebound LiteLLM's global
    # worker and the first loop's task was destroyed while pending.
    main = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            main.run_until_complete(_mock_call())
            worker = threading.Thread(target=lambda: asyncio.run(_mock_call()))
            worker.start()
            worker.join()
            main.run_until_complete(_mock_call())
            gc.collect()
    finally:
        main.close()

    assert "Task was destroyed but it is pending" not in caplog.text
    assert GLOBAL_LOGGING_WORKER._worker_task is None


async def test_a_registered_callback_still_uses_the_sdk_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[bool] = []

    async def handler() -> None:
        ran.append(True)

    monkeypatch.setattr(litellm, "callbacks", [object()])
    GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue(handler())
    try:
        assert GLOBAL_LOGGING_WORKER._worker_task is not None
        await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), 5)
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
    assert ran == [True]


def test_without_callbacks_the_handler_is_closed_not_queued() -> None:
    async def handler() -> Any:
        raise AssertionError("must not run")

    coroutine = handler()
    GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue(coroutine)
    assert coroutine.cr_frame is None  # closed
    assert GLOBAL_LOGGING_WORKER._worker_task is None
