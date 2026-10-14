import functools
import inspect
import logging
import os
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from Bio import Entrez
from Bio.Entrez import Parser

from mcp_server.workers import run_worker

logger = logging.getLogger(__name__)
MAX_ENTREZ_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ENTREZ_TOOL_BYTES = 16 * 1024 * 1024


@dataclass
class _ByteBudget:
    remaining: int = MAX_ENTREZ_TOOL_BYTES
    lock: threading.Lock = field(default_factory=threading.Lock)

    def charge(self, count: int) -> None:
        with self.lock:
            self.remaining -= count
            if self.remaining < 0:
                self.remaining = 0
                raise ValueError("Entrez aggregate byte budget exceeded")


_byte_budget: ContextVar[_ByteBudget | None] = ContextVar("entrez_byte_budget", default=None)


def _charge_bytes(count: int) -> None:
    if budget := _byte_budget.get():
        budget.charge(count)


def budgeted_entrez(fn: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_call(*args: Any, **kwargs: Any) -> Any:
            token = _byte_budget.set(_ByteBudget())
            try:
                return await fn(*args, **kwargs)
            finally:
                _byte_budget.reset(token)

        return async_call

    @functools.wraps(fn)
    def sync_call(*args: Any, **kwargs: Any) -> Any:
        token = _byte_budget.set(_ByteBudget())
        try:
            return fn(*args, **kwargs)
        finally:
            _byte_budget.reset(token)

    return sync_call


async def entrez_worker(fn: Callable[..., Any], *args: Any) -> Any:
    return await run_worker(fn, *args)


class _BoundedResponse:
    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self._remaining = MAX_ENTREZ_RESPONSE_BYTES

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def close(self) -> None:
        self._handle.close()

    def read(self, size: int = -1) -> bytes:
        count = self._remaining + 1 if size < 0 else min(size, self._remaining + 1)
        data: bytes = self._handle.read(count)
        self._remaining -= len(data)
        if self._remaining < 0:
            raise ValueError("Entrez response byte budget exceeded")
        _charge_bytes(len(data))
        return data


def read_entrez_bytes(handle: Any, maximum: int = MAX_ENTREZ_RESPONSE_BYTES) -> bytes:
    try:
        data: bytes = handle.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Entrez response byte budget exceeded")
        _charge_bytes(len(data))
        return data
    finally:
        handle.close()


def read_entrez(handle: Any) -> Any:
    """Pace before sending requests, not while parsing their responses."""
    try:
        return Entrez.read(_BoundedResponse(handle))
    except Exception:
        logger.debug("entrez response parsing failed")
        raise
    finally:
        handle.close()


# Biopython stores credentials process-wide, so configure once across tool
# modules.
_entrez_initialized = False


def initialize_entrez() -> None:
    """NCBI rejects empty api_key parameters with HTTP 400; omit unset keys."""
    global _entrez_initialized

    if os.environ.get("DISABLE_SSL_VERIFY", "").lower() in (
        "true",
        "1",
        "yes",
    ):
        raise RuntimeError("Disabling TLS verification is unsupported")

    if _entrez_initialized:
        return

    # Set the guard first so failed initialization cannot repeat warnings on
    # every import.
    _entrez_initialized = True
    _init_entrez_email()
    _init_entrez_api_key()


def _init_entrez_email() -> None:
    if Entrez.email:
        return

    entrez_email = os.environ.get("ENTREZ_EMAIL")
    if entrez_email:
        Entrez.email = entrez_email
        logger.info("Initialized Entrez contact email")
    else:
        # NCBI asks for a contact email to identify traffic; requests
        # still work without one but may be throttled more readily.
        logger.warning("ENTREZ_EMAIL not set - PubMed may have stricter rate limits")


def _init_entrez_api_key() -> None:
    if Entrez.api_key:
        return

    entrez_key = os.environ.get("ENTREZ_API_KEY")
    if entrez_key:
        Entrez.api_key = entrez_key
        logger.info("Initialized Entrez with API key")
    else:
        # NCBI permits 3 requests/s without a key, 10 with one.
        logger.info("ENTREZ_API_KEY not set - using default rate limits")


# Pace the entire process just below NCBI's 10/s keyed and 3/s unkeyed ceilings.
_INTERVAL_WITH_API_KEY = 0.11
_INTERVAL_WITHOUT_API_KEY = 0.4

_lock = threading.Lock()
_next_slot = 0.0

# Indirect clock/sleep hooks let tests avoid real elapsed waits.
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


def _request_interval() -> float:
    initialize_entrez()
    return _INTERVAL_WITH_API_KEY if Entrez.api_key else _INTERVAL_WITHOUT_API_KEY


def _await_slot() -> None:
    interval = _request_interval()
    with _lock:
        now = _clock()
        start = max(now, _next_slot)
        _claim_slot(start + interval)
    delay = start - now
    if delay > 0:
        _sleep(delay)


def _claim_slot(next_slot: float) -> None:
    """The caller must hold the process-wide pacer lock."""
    global _next_slot
    _next_slot = next_slot


def entrez_call(request: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Every Entrez request must use this seam; pacing happens per send below."""
    budget = _byte_budget.get()
    if budget is not None and budget.remaining <= 0:
        raise ValueError("Entrez aggregate byte budget exceeded")
    install_send_gate()
    return request(**kwargs)


_unpaced_urlopen: Callable[..., Any] | None = None


def _paced_urlopen(*args: Any, **kwargs: Any) -> Any:
    assert _unpaced_urlopen is not None
    _await_slot()
    kwargs.setdefault("timeout", 30)
    return _unpaced_urlopen(*args, **kwargs)


def install_send_gate() -> None:
    """Biopython retries 429s and 5xx inside `Entrez._open`, and its parser
    fetches missing DTDs; pacing whole calls let those sends burst past NCBI's
    per-second limit, so every physical urlopen takes a process-wide slot."""
    global _unpaced_urlopen
    if Entrez.urlopen is _paced_urlopen:
        return
    _unpaced_urlopen = Entrez.urlopen
    Entrez.urlopen = _paced_urlopen
    Parser.urlopen = _paced_urlopen


install_send_gate()
