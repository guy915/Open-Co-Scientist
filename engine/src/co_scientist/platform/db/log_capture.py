from __future__ import annotations

import contextlib
import hashlib
import logging
import logging.handlers
import sys
import threading

from co_scientist.platform.db import logs as store
from co_scientist.platform.db.logs import NewLogRecord
from co_scientist.platform.telemetry.capture_queue import CaptureListener, CaptureQueue
from co_scientist.platform.telemetry.logging_setup import (
    RunIdFilter,
    _byok_redaction_filter,
    diagnostic_projection,
    is_orphaned_litellm_worker,
)
from co_scientist.platform.telemetry.tracing import private_error_type

_PRUNE_EVERY = 500
DEFAULT_LOG_MAX_ROWS = 20_000
_EXC_FORMATTER = logging.Formatter()


class _CaptureQueueHandler(logging.handlers.QueueHandler):
    """Materialize message, exception and run context on the emitting thread
    before the listener crosses that boundary.
    """

    queue: CaptureQueue

    def handle(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_cosci_captured", False):
            return False
        record._cosci_captured = True
        # Filters (including repeat history) must only retain bounded copies.
        return super().handle(self.prepare(record))

    def emit(self, record: logging.LogRecord) -> None:
        self.enqueue(record)

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Copy shared records before materialization; malformed logging
        arguments must not break capture.
        """
        record = diagnostic_projection(record)
        try:
            message = record.getMessage()
        except Exception:
            message = "log message could not be formatted"
        exc_text = record.exc_text
        if record.exc_info and not record.exc_text:
            with contextlib.suppress(Exception):
                exc_text = _EXC_FORMATTER.formatException(record.exc_info)
        from co_scientist.core.byok_scope import redact_byok_text

        pending = logging.LogRecord(
            _bounded_text(record.name, 256),
            record.levelno,
            _bounded_text(record.pathname, 256),
            record.lineno,
            _bounded_text(redact_byok_text(message), 8192),
            (),
            None,
        )
        pending.created = record.created
        pending.exc_text = _bounded_text(redact_byok_text(exc_text), 8192) if exc_text else None
        owner = getattr(record, "client_id", None)
        pending.client_id = _bounded_text(owner, 128) if isinstance(owner, str) else None
        return pending


def _bounded_text(text: str, byte_limit: int) -> str:
    return text.encode("utf-8", errors="replace")[:byte_limit].decode("utf-8", errors="ignore")


class _StoreWriteHandler(logging.Handler):
    """Persistence failures must not enter the logging path; warn once
    rather than creating another record per failure.
    """

    def __init__(self, max_rows: int) -> None:
        super().__init__()
        self._max_rows = max_rows
        self._writes = 0
        self._warned = False
        # A bounded shutdown may leave one busy write finishing; it must never
        # spill into a later configuration's database.
        self._db_path = store.bound_log_db_path()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            store.append_log(
                NewLogRecord(
                    level=record.levelname,
                    levelno=record.levelno,
                    logger_name=record.name,
                    message=record.getMessage(),
                    run_id=getattr(record, "run_id", None),
                    exc_text=record.exc_text,
                    created_at=record.created,
                    client_id=getattr(record, "client_id", None),
                ),
                db_path=self._db_path,
            )
            self._writes += 1
            if self._writes == 1 or self._writes % _PRUNE_EVERY == 0:
                store.prune_capture_logs(max_rows=self._max_rows, db_path=self._db_path)
        except Exception as exc:  # must never propagate into logging
            if not self._warned:
                self._warned = True
                print(
                    "cosci: log capture write failed "
                    f"({private_error_type(type(exc).__name__)}); further failures suppressed",
                    file=sys.stderr,
                )


# Attach directly to uvicorn's non-propagating loggers so access and server
# errors persist.
_EXTRA_CAPTURE_LOGGERS = ("uvicorn", "uvicorn.access")


# Discard per-call chatter before it starves SQLite's writer; read-time verbose
# filtering is separate.
UNPERSISTED_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "urllib3",
    "litellm",
    "openai",
    "mcp.client",
    # Availability warnings remain visible even when ordinary dependency chatter
    # is dropped.
    "co_scientist.platform.retrieval.mcp_client",
)


def _drop_dependency_chatter(record: logging.LogRecord) -> bool:
    """Logger names and children vary in casing, including LiteLLM and MCP
    transport modules.
    """
    if record.levelno >= logging.WARNING:
        return True
    name = record.name.lower()
    return not any(name == noise or name.startswith(f"{noise}.") for noise in UNPERSISTED_LOGGERS)


# Periodic resurfacing preserves ongoing-condition visibility without filling
# the bounded log window.
REPEAT_SUPPRESS_SECONDS = 600.0

# Bound repeat history even when messages never repeat.
_REPEAT_KEYS_MAX = 2_000


class _RepeatSuppressor:
    """Suppress exact repeats before writes so steady warnings cannot crowd
    real records out of the bounded log window.
    """

    def __init__(self, window: float = REPEAT_SUPPRESS_SECONDS) -> None:
        self._window = window
        self._seen: dict[tuple[str, int, str | None, bytes], float] = {}
        self._lock = threading.Lock()

    def __call__(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        key = (
            record.name,
            record.levelno,
            getattr(record, "run_id", None),
            hashlib.sha256(message.encode()).digest(),
        )
        now = record.created
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < self._window:
                return False
            if len(self._seen) >= _REPEAT_KEYS_MAX:
                self._prune(now)
            self._seen[key] = now
        return True

    def _prune(self, now: float) -> None:
        """Bound suppression history even when every key is unique and
        nothing has yet expired.
        """
        self._seen = {key: seen for key, seen in self._seen.items() if now - seen < self._window}
        if len(self._seen) >= _REPEAT_KEYS_MAX:
            self._seen.clear()


def _drop_orphaned_logging_worker_noise(record: logging.LogRecord) -> bool:
    """Orphaned library logging workers remain visible on stdout, without
    being attributed as scientific run errors.
    """
    return not is_orphaned_litellm_worker(record)


def _drop_self_noise(record: logging.LogRecord) -> bool:
    # Polling logs must not grow them through their own access records.
    if record.name != "uvicorn.access":
        return True
    try:
        return "/api/logs" not in record.getMessage()
    except Exception:
        return True


class LogCapture:
    def __init__(
        self,
        handler: _CaptureQueueHandler,
        listener: logging.handlers.QueueListener,
    ) -> None:
        self._handler = handler
        self._listener = listener
        self._stopped = False
        self._queue: CaptureQueue = handler.queue

    def stats(self) -> dict[str, int]:
        return self._queue.stats()

    def stop(self) -> None:
        """QueueListener.stop is not idempotent, but explicit shutdown and
        reconfiguration can both stop the same pipeline.
        """
        if self._stopped:
            return
        self._stopped = True
        logging.getLogger().removeHandler(self._handler)
        for name in _EXTRA_CAPTURE_LOGGERS:
            logging.getLogger(name).removeHandler(self._handler)
        # QueueListener.stop drains queued records before joining its writer
        # thread.
        self._listener.stop()


_capture: LogCapture | None = None


def _build_capture_pipeline(
    level: int, max_rows: int
) -> tuple[_CaptureQueueHandler, CaptureListener]:
    record_queue = CaptureQueue()
    handler = _CaptureQueueHandler(record_queue)
    handler.setLevel(level)
    handler.addFilter(RunIdFilter())
    handler.addFilter(_byok_redaction_filter())
    handler.addFilter(_drop_self_noise)
    handler.addFilter(_drop_dependency_chatter)
    handler.addFilter(_drop_orphaned_logging_worker_noise)
    # Suppress repeats after stamping run IDs because run identity belongs in
    # the suppression key.
    handler.addFilter(_RepeatSuppressor())
    listener = CaptureListener(record_queue, _StoreWriteHandler(max_rows))
    return handler, listener


def _attach_capture_handler(handler: _CaptureQueueHandler) -> None:
    logging.getLogger().addHandler(handler)
    # Shared record marks prevent duplicate capture when directly attached
    # uvicorn loggers also propagate.
    for name in _EXTRA_CAPTURE_LOGGERS:
        logging.getLogger(name).addHandler(handler)


def configure_log_capture(
    level: int = logging.INFO, max_rows: int = DEFAULT_LOG_MAX_ROWS
) -> LogCapture:
    """Drain and detach the previous pipeline before replacing it; repeated
    setup must never duplicate records.
    """
    global _capture
    shutdown_log_capture()
    handler, listener = _build_capture_pipeline(level, max_rows)
    listener.start()
    _attach_capture_handler(handler)
    _capture = LogCapture(handler, listener)
    return _capture


def shutdown_log_capture() -> None:
    global _capture
    if _capture is not None:
        _capture.stop()
        _capture = None
