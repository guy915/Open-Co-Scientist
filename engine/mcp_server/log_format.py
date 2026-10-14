import json
import logging
import sys

from mcp_server.framework_log_privacy import FrameworkLogPrivacy, install_framework_log_privacy

# Uvicorn and FastMCP install their own stderr handlers, which the host files
# at error severity line by line; route them through root's JSON stdout
# handler, where the transport privacy filter also sees every record.
_ROUTED_LOGGERS = ("uvicorn", "uvicorn.access", "fastmcp")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = record.exc_text or self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_json_logging() -> None:
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_mcp_json_handler", False):
            root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout)
    handler._mcp_json_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    handler.addFilter(FrameworkLogPrivacy())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)
    install_framework_log_privacy()


def route_library_loggers() -> None:
    for name in _ROUTED_LOGGERS:
        library_logger = logging.getLogger(name)
        for handler in list(library_logger.handlers):
            library_logger.removeHandler(handler)
        library_logger.propagate = True
