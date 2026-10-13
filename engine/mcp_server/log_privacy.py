import logging

_TRANSPORT_LOGGERS = ("httpx", "httpcore", "fastmcp", "mcp")


class TransportLogPrivacy(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if any(
            record.name == name or record.name.startswith(name + ".") for name in _TRANSPORT_LOGGERS
        ):
            # SDK diagnostics can include request URLs, validation inputs and
            # exception locals even when the tool wrapper logs only metadata.
            record.msg = "MCP transport diagnostic (private details omitted)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def install_transport_log_privacy() -> None:
    loggers = [logging.getLogger(), *(logging.getLogger(name) for name in _TRANSPORT_LOGGERS)]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(item, TransportLogPrivacy) for item in handler.filters):
                # FastMCP has non-propagating Rich handlers as well as root
                # transport output; run before their traceback selection.
                handler.filters.insert(0, TransportLogPrivacy())


# Exception text and URLs can carry query terms or configured keys, so failure
# logs name only an allowlisted class and a numeric HTTP status.
_LOGGABLE_FAILURE_CLASSES = frozenset(
    {
        "AttributeError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "HTTPError",
        "HTTPStatusError",
        "IndexError",
        "JSONDecodeError",
        "KeyError",
        "ParseError",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimeoutError",
        "TimeoutException",
        "TypeError",
        "URLError",
        "ValueError",
        "WriteTimeout",
    }
)


def _http_status(exc: BaseException) -> int | None:
    candidates = (
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    )
    for candidate in candidates:
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and 100 <= candidate <= 599
        ):
            return candidate
    return None


def failure_summary(exc: BaseException) -> str:
    name = type(exc).__name__
    summary = name if name in _LOGGABLE_FAILURE_CLASSES else "other error"
    status = _http_status(exc)
    return f"{summary}, HTTP {status}" if status is not None else summary
