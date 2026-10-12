from __future__ import annotations

import contextlib
import copy
import json
import logging
import logging.handlers
import re
import sys
import traceback
from collections.abc import Generator
from contextvars import ContextVar

from co_scientist.platform.telemetry.tracing import current_trace_id, private_error_type

_PRIVATE_LOGGERS = (
    "co_scientist.",
    "app.run_stage",
    "litellm",
    "LiteLLM",
    "openai",
    "httpx",
    "httpcore",
)
_DIAGNOSTIC_EVENTS = frozenset(
    {
        "lifecycle",
        "stage_start",
        "stage_end",
        "rate_limit",
        "report",
        "status",
        "scientific_task",
        "supervisor.plan",
        "safety.final",
        "safety.hypothesis",
        "citation.grounding",
        "citation_audit",
        "schema_degraded",
    }
)


def diagnostic_projection(record: logging.LogRecord) -> logging.LogRecord:
    projected = copy.copy(record)
    if record.name.startswith(_PRIVATE_LOGGERS):
        # Free-form prose and exception values cannot be distinguished from
        # private research. Export only source location and numeric metadata.
        values = record.args.values() if isinstance(record.args, dict) else record.args or ()
        numbers = [value for value in values if type(value) in (int, float, bool)][:16]
        event = getattr(record, "diagnostic_event", None)
        event = event if isinstance(event, str) and event in _DIAGNOSTIC_EVENTS else "other"
        projected.msg = "Research diagnostic event=%s at line %s; numeric values=%s"
        projected.args = (event, record.lineno, numbers)
        projected.stack_info = None
    if record.name.startswith(_PRIVATE_LOGGERS) or record.name.startswith("uvicorn"):
        if record.exc_info and record.exc_info[1] is not None:
            projected.exc_text = private_error_type(type(record.exc_info[1]).__name__)
        elif record.exc_text:
            projected.exc_text = "Exception details withheld for privacy"
        projected.exc_info = None
        projected.stack_info = None
    return projected


# Child tasks inherit run context, allowing one workflow binding to correlate
# every emitted record.
_run_id_var: ContextVar[str | None] = ContextVar("cosci_run_id", default=None)


def current_run_id() -> str | None:
    return _run_id_var.get()


@contextlib.contextmanager
def run_log_context(run_id: str) -> Generator[None, None, None]:
    token = _run_id_var.set(run_id)
    try:
        yield
    finally:
        _run_id_var.reset(token)


class RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id_var.get()
        record.trace_id = current_trace_id()
        return True


# LiteLLM's logging worker orphans its task whenever another event loop rebinds
# it, and asyncio reports the unreachable task at ERROR. No hook this app owns
# can reach it, and it never affects a completion. Identity comes from the
# parsed task repr, never from exception text, so application failures that
# mention the worker stay errors.
_ORPHANED_LITELLM_WORKER_NOTE = "orphaned LiteLLM logging worker task; completions are unaffected"
_LITELLM_WORKER_TASK = re.compile(
    r"(?:task|future): <Task (?:pending|finished|cancelled) "
    r"name=(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\") "
    r"coro=<LoggingWorker\.(?P<coroutine>_worker_loop|_process_log_task)\(\) "
    r"(?:running at|done, defined at) \S*/litellm/litellm_core_utils/logging_worker\.py:\d+>"
)
_DESTROYED_PENDING = "Task was destroyed but it is pending!"
_NEVER_RETRIEVED = "Task exception was never retrieved"
_WORKER_QUEUE_UNDERFLOW = "ValueError: task_done() called too many times"


def _exception_summary(record: logging.LogRecord) -> str | None:
    if record.exc_info and record.exc_info[1] is not None:
        return "".join(traceback.format_exception_only(record.exc_info[1])).strip()
    if record.exc_text:
        return record.exc_text.rstrip().rsplit("\n", 1)[-1]
    return None


def is_orphaned_litellm_worker(record: logging.LogRecord) -> bool:
    if record.name != "asyncio":
        return False
    try:
        message = record.getMessage()
    except Exception:
        return False
    headline, _, detail = message.partition("\n")
    task = _LITELLM_WORKER_TASK.match(detail.partition("\n")[0])
    if task is None:
        return False
    if headline == _DESTROYED_PENDING:
        return task["coroutine"] == "_worker_loop" and _exception_summary(record) is None
    if headline == _NEVER_RETRIEVED:
        return (
            task["coroutine"] == "_process_log_task"
            and _exception_summary(record) == _WORKER_QUEUE_UNDERFLOW
        )
    return False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record = diagnostic_projection(record)
        level = record.levelname
        orphaned_worker = record.levelno > logging.WARNING and is_orphaned_litellm_worker(record)
        if orphaned_worker:
            level = logging.getLevelName(logging.WARNING)
        payload: dict[str, object] = {
            "time": self.formatTime(record),
            "level": level,
            "logger": record.name,
            "message": record.getMessage(),
        }
        run_id = getattr(record, "run_id", None)
        if run_id:
            payload["run_id"] = run_id
        trace_id = getattr(record, "trace_id", None)
        if trace_id:
            payload["trace_id"] = trace_id
        if record.exc_info:
            payload["exc_info"] = record.exc_text or self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc_info"] = record.exc_text
        if orphaned_worker:
            payload["note"] = _ORPHANED_LITELLM_WORKER_NOTE
        return json.dumps(payload, default=str)


# LiteLLM installs its own handler; root levels and LITELLM_LOG do not stop
# duplicate propagation, so set logger levels directly.
_LITELLM_LOGGER_NAMES: tuple[str, ...] = (
    "LiteLLM",
    "LiteLLM Router",
    "LiteLLM Proxy",
)


def silence_litellm_logging() -> None:
    """LiteLLM's own logger handlers bypass root levels."""
    for name in _LITELLM_LOGGER_NAMES:
        sdk_logger = logging.getLogger(name)
        sdk_logger.setLevel(logging.WARNING)
        for handler in list(sdk_logger.handlers):
            if isinstance(handler, logging.StreamHandler):
                sdk_logger.removeHandler(handler)
        sdk_logger.propagate = True


silence_litellm_logging()


# Uvicorn's own config writes lifecycle lines and ASGI tracebacks to stderr as
# plain text, which the host files at error severity line by line. Only the
# parents carry handlers; "uvicorn.error" propagates to "uvicorn".
_UVICORN_HANDLER_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.access")


def _route_uvicorn_logging(handler: logging.Handler) -> None:
    """Replace only stream handlers: log capture attaches its own queue
    handler to these loggers and must survive a reconfiguration.
    """
    for name in _UVICORN_HANDLER_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        for existing in list(uvicorn_logger.handlers):
            if isinstance(existing, logging.StreamHandler):
                uvicorn_logger.removeHandler(existing)
        uvicorn_logger.addHandler(handler)
        uvicorn_logger.propagate = False


def _byok_redaction_filter() -> logging.Filter:
    """Lazy import avoids pulling credential cryptography into logging-only
    processes; provider errors still need redaction.
    """
    from co_scientist.core.byok_scope import ByokRedactionFilter

    return ByokRedactionFilter()


def configure_logging(level: int = logging.INFO) -> logging.Handler:
    """Replace the previously installed handler so reloads cannot duplicate
    every record.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_cosci_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler._cosci_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunIdFilter())
    handler.addFilter(_byok_redaction_filter())
    root.addHandler(handler)
    root.setLevel(level)
    _route_uvicorn_logging(handler)
    silence_litellm_logging()
    return handler


def level_to_number(name: str) -> int | None:
    """getLevelNamesMapping is Python 3.11+; getLevelName also distinguishes
    unknown names on older interpreters.
    """
    value = logging.getLevelName(name.upper())
    return value if isinstance(value, int) else None


__all__ = ["run_log_context", "silence_litellm_logging"]
