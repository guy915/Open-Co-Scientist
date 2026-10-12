from __future__ import annotations

import asyncio
import gc
import importlib.util
import io
import json
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from co_scientist.platform.db.log_capture import (
    _CaptureQueueHandler,
    _drop_orphaned_logging_worker_noise,
)
from co_scientist.platform.telemetry.capture_queue import CaptureQueue
from co_scientist.platform.telemetry.logging_setup import configure_logging

_WORKER_SOURCE = """
import asyncio


class LoggingWorker:
    failure = ValueError("task_done() called too many times")

    async def _worker_loop(self):
        await asyncio.Event().wait()

    async def _process_log_task(self):
        raise self.failure
"""


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _asyncio_records(start: Callable[[asyncio.AbstractEventLoop], None]) -> list[logging.LogRecord]:
    """Let asyncio itself report an abandoned task, as production saw it."""
    collect = _Collect()
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.addHandler(collect)
    try:
        loop = asyncio.new_event_loop()
        start(loop)
        loop.close()
        gc.collect()
    finally:
        asyncio_logger.removeHandler(collect)
    return [record for record in collect.records if record.levelno >= logging.ERROR]


# Factories, not coroutines: a caller holding the coroutine keeps the
# abandoned task reachable past the collection that reports it.
_Factory = Callable[[], Coroutine[Any, Any, Any]]


def _destroyed_pending(factory: _Factory, name: str | None = None) -> logging.LogRecord:
    def start(loop: asyncio.AbstractEventLoop) -> None:
        loop.create_task(factory(), name=name)
        loop.run_until_complete(asyncio.sleep(0))

    [record] = _asyncio_records(start)
    return record


def _never_retrieved(factory: _Factory) -> logging.LogRecord:
    def start(loop: asyncio.AbstractEventLoop) -> None:
        loop.create_task(factory())
        loop.run_until_complete(asyncio.sleep(0.01))

    [record] = _asyncio_records(start)
    return record


@pytest.fixture
def litellm_worker(tmp_path: Path) -> Any:
    source = tmp_path / "litellm" / "litellm_core_utils" / "logging_worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(_WORKER_SOURCE)
    spec = importlib.util.spec_from_file_location("orphaned_worker_fixture", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LoggingWorker()


def _stdout_payload(record: logging.LogRecord) -> dict[str, Any]:
    handler = configure_logging()
    stream = io.StringIO()
    handler.stream = stream  # type: ignore[attr-defined]
    try:
        handler.handle(record)
    finally:
        configure_logging()
    payload: dict[str, Any] = json.loads(stream.getvalue().splitlines()[-1])
    return payload


def _captured(record: logging.LogRecord) -> bool:
    materialized = _CaptureQueueHandler(CaptureQueue()).prepare(record)
    return _drop_orphaned_logging_worker_noise(materialized)


def test_destroyed_litellm_worker_loop_is_a_warning_and_not_captured(litellm_worker: Any) -> None:
    record = _destroyed_pending(litellm_worker._worker_loop)

    payload = _stdout_payload(record)

    assert payload["level"] == "WARNING"
    assert "LiteLLM logging worker" in payload["note"]
    assert _captured(record) is False


def test_litellm_worker_queue_underflow_is_a_warning_and_not_captured(
    litellm_worker: Any,
) -> None:
    record = _never_retrieved(litellm_worker._process_log_task)

    payload = _stdout_payload(record)

    assert payload["level"] == "WARNING"
    assert "task_done() called too many times" in payload["exc_info"]
    assert _captured(record) is False


async def _application_worker() -> None:
    raise RuntimeError(
        "LoggingWorker._worker_loop() running at "
        "/x/litellm/litellm_core_utils/logging_worker.py:1> task_done() called too many times"
    )


async def _waiting_application_task() -> None:
    await asyncio.Event().wait()


def test_application_error_mentioning_the_worker_stays_an_error() -> None:
    record = _never_retrieved(_application_worker)

    payload = _stdout_payload(record)

    assert payload["level"] == "ERROR"
    assert "note" not in payload
    assert _captured(record) is True


def test_application_task_named_like_the_worker_stays_an_error() -> None:
    spoofed_name = (
        "x' coro=<LoggingWorker._worker_loop() running at "
        "/x/litellm/litellm_core_utils/logging_worker.py:1> '"
    )
    record = _destroyed_pending(_waiting_application_task, name=spoofed_name)

    payload = _stdout_payload(record)

    assert payload["level"] == "ERROR"
    assert _captured(record) is True


def test_other_litellm_worker_failures_stay_errors(litellm_worker: Any) -> None:
    litellm_worker.failure = RuntimeError("cannot reuse already awaited coroutine")
    record = _never_retrieved(litellm_worker._process_log_task)

    assert _stdout_payload(record)["level"] == "ERROR"
    assert _captured(record) is True


def test_capture_queue_drops_only_the_bounded_worker_shapes(litellm_worker: Any) -> None:
    worker = _destroyed_pending(litellm_worker._worker_loop)
    application = _never_retrieved(_application_worker)

    kept = [
        record.getMessage().partition("\n")[0]
        for record in (worker, application)
        if _captured(record)
    ]

    assert kept == ["Task exception was never retrieved"]
