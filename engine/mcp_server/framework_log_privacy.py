import logging

_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
)
_LIFECYCLE_MESSAGES = frozenset(
    {
        "Waiting for application startup.",
        "Application startup complete.",
        "Application startup failed. Exiting.",
        "Shutting down",
        "Waiting for connections to close. (CTRL+C to force quit)",
        "Waiting for background tasks to complete. (CTRL+C to force quit)",
        "Waiting for application shutdown.",
        "Application shutdown complete.",
        "ASGI 'lifespan' protocol appears unsupported.",
    }
)
_PROCESS_MESSAGES = frozenset({"Started server process [%d]", "Finished server process [%d]"})


class FrameworkLogPrivacy(logging.Filter):
    def __init__(self, *, keep_access_args: bool = False) -> None:
        super().__init__()
        self._keep_access_args = keep_access_args

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn" and not record.name.startswith("uvicorn."):
            return True
        args = record.args
        if record.name == "uvicorn.access":
            method = "UNKNOWN"
            status: int | str = "UNKNOWN"
            if isinstance(args, tuple) and len(args) == 5:
                candidate_method, candidate_status = args[1], args[4]
            elif (
                record.msg == "MCP HTTP request method=%s status=%s"
                and isinstance(args, tuple)
                and len(args) == 2
            ):
                candidate_method, candidate_status = args
            else:
                candidate_method, candidate_status = None, None
            if isinstance(candidate_method, str) and candidate_method in _METHODS:
                method = candidate_method
            if type(candidate_status) is int and 100 <= candidate_status <= 599:
                status = candidate_status
            if self._keep_access_args:
                # Uvicorn's access formatter unpacks all five fields even
                # after a logging reconfiguration replaces our JSON handler.
                record.msg = '%s - "%s %s HTTP/%s" %d'
                record.args = (
                    "MCP",
                    method,
                    "[private-target]",
                    "unknown",
                    status if type(status) is int else 0,
                )
            else:
                record.msg = "MCP HTTP request method=%s status=%s"
                record.args = (method, status)
        elif (isinstance(record.msg, str) and record.msg in _LIFECYCLE_MESSAGES and not args) or (
            isinstance(record.msg, str)
            and record.msg in _PROCESS_MESSAGES
            and isinstance(args, tuple)
            and len(args) == 1
            and type(args[0]) is int
            and 0 < args[0] < 2**63
        ):
            pass
        else:
            record.msg = "MCP server diagnostic (private details omitted)"
            record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        # Uvicorn's colored formatter otherwise substitutes this alternate
        # message after the projected message has passed the logger filter.
        record.__dict__.pop("color_message", None)
        return True


def install_framework_log_privacy() -> None:
    # Logger filters survive Uvicorn's programmatic logging reconfiguration;
    # the handler filter also covers future child logger names.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, FrameworkLogPrivacy) for item in logger.filters):
            logger.addFilter(FrameworkLogPrivacy(keep_access_args=True))
