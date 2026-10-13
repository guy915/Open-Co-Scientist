"""Spans stay off unless an OTLP endpoint is configured; span names
and attributes are an operator contract, and never carry prompt or user text.
"""

from __future__ import annotations

import builtins
import contextlib
import logging
import os
import re
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import unquote

from opentelemetry import trace

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter

logger = logging.getLogger(__name__)

_TRACER_NAME: Final[str] = "co_scientist"
_ENDPOINT_ENV: Final[str] = "OTEL_EXPORTER_OTLP_ENDPOINT"
_DISABLED_ENV: Final[str] = "OTEL_SDK_DISABLED"
_DEFAULT_SERVICE_NAME: Final[str] = "co-scientist-api"
OTLP_HEADER_ENV_VARS: Final = (
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
)
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEADER_VALUE = re.compile(r"[\x20-\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*")


def _valid_otlp_headers() -> bool:
    # The SDK logs malformed headers verbatim. Validate before invoking its parser.
    for name in OTLP_HEADER_ENV_VARS:
        for header in os.environ.get(name, "").split(","):
            if not header.strip():
                continue
            key, separator, value = header.strip().partition("=")
            if (
                not separator
                or not _HEADER_NAME.fullmatch(key.strip())
                or not _HEADER_NAME.fullmatch(unquote(key).strip())
                or not _HEADER_VALUE.fullmatch(value.strip())
                or any(not 32 <= ord(char) <= 126 for char in unquote(value))
            ):
                return False
    return True


class _Provider(Protocol):
    def get_tracer(self, instrumenting_module_name: str) -> trace.Tracer: ...

    def shutdown(self) -> None: ...


# Kept off the OpenTelemetry global so tests can swap providers per case.
_provider: _Provider | None = None
_NOOP_TRACER: Final[trace.Tracer] = trace.NoOpTracer()


def tracer() -> trace.Tracer:
    if _provider is None:
        return _NOOP_TRACER
    return _provider.get_tracer(_TRACER_NAME)


def tracing_requested() -> bool:
    if os.environ.get(_DISABLED_ENV, "").strip().lower() == "true":
        return False
    return bool(os.environ.get(_ENDPOINT_ENV, "").strip())


def configure_tracing() -> bool:
    global _provider
    if _provider is not None or not tracing_requested():
        return _provider is not None
    if not _valid_otlp_headers():
        logger.warning("Tracing disabled: invalid OTLP header configuration")
        return False
    # The SDK and exporter ship only in the api image.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service = os.environ.get("OTEL_SERVICE_NAME") or _DEFAULT_SERVICE_NAME
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service}))
    provider.add_span_processor(BatchSpanProcessor(private_exporter(OTLPSpanExporter())))
    _provider = provider
    logger.info("tracing enabled for service %s", service)
    return True


def shutdown_tracing() -> None:
    global _provider
    provider, _provider = _provider, None
    if provider is not None:
        provider.shutdown()


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return trace.format_trace_id(context.trace_id)


_ERROR_TYPES = frozenset(
    name
    for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
) | frozenset(
    {
        "ContextWindowExceededError",
        "RateLimitError",
        "AuthenticationError",
        "BadRequestError",
        "APIConnectionError",
        "BudgetExceededError",
        "LLMTimeoutError",
    }
)


def private_error_type(value: Any) -> str:
    return value if isinstance(value, str) and value in _ERROR_TYPES else "Exception"


def mark_error(span: trace.Span, error: BaseException) -> None:
    """Exception messages can quote provider or user text; record the type
    only.
    """
    span.set_attribute("error.type", private_error_type(type(error).__name__))
    span.set_status(trace.StatusCode.ERROR)


@contextlib.contextmanager
def current_span(
    name: str, attributes: dict[str, Any], kind: trace.SpanKind = trace.SpanKind.INTERNAL
) -> Iterator[trace.Span]:
    # The SDK's own exception recording would copy the message into the span.
    with tracer().start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            mark_error(span, error)
            raise


_NUMERIC_ATTRIBUTES = frozenset(
    {
        "http.response.status_code",
        "gen_ai.request.max_tokens",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "co_scientist.llm.reasoning_tokens",
        "co_scientist.llm.cached_prompt_tokens",
        "co_scientist.llm.cache_write_tokens",
        "co_scientist.llm.call_type",
        "co_scientist.llm.refusal",
        "co_scientist.llm.attempt",
        "co_scientist.task.attempt",
    }
)
_ID_ATTRIBUTES = frozenset({"co_scientist.run_id", "co_scientist.task.id"})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_SPAN_NAMES = frozenset(
    {"task.execute", "llm.call_llm", "llm.call_llm_json", "llm.call_llm_with_tools", "llm.attempt"}
)


def _private_span(span: ReadableSpan) -> ReadableSpan:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan

    attributes: dict[str, Any] = {}
    for key, value in (span.attributes or {}).items():
        if (
            key in _NUMERIC_ATTRIBUTES
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ) or (
            key in _ID_ATTRIBUTES
            and isinstance(value, str)
            and re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", value)
        ):
            attributes[key] = value
        elif key in {"error.type", "co_scientist.llm.retry_reason"}:
            attributes[key] = private_error_type(value)
        elif (
            (key == "http.request.method" and isinstance(value, str) and value in _METHODS)
            or (key == "gen_ai.operation.name" and value == "chat")
            or (
                key == "co_scientist.task.outcome"
                and isinstance(value, str)
                and value
                in {
                    "completed",
                    "cancelled",
                    "failed",
                    "lease_lost",
                    "retry_scheduled",
                    "expired",
                    "skipped",
                }
            )
        ):
            attributes[key] = value
    name = span.name if span.name in _SPAN_NAMES else "operation"
    if span.name.startswith("chat "):
        name = "llm.request"
    elif "http.request.method" in attributes:
        name = "HTTP " + attributes["http.request.method"]
    # Custom model IDs, response.model and SDK resource/event fields can echo
    # private text. The final boundary accepts fixed classifications only.
    return ReadableSpan(
        name=name,
        context=span.context,
        parent=span.parent,
        resource=Resource({"service.name": _DEFAULT_SERVICE_NAME}),
        attributes=attributes,
        kind=span.kind,
        status=trace.Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
    )


def private_exporter(delegate: SpanExporter) -> SpanExporter:
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class PrivateExporter(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            return delegate.export(tuple(_private_span(span) for span in spans))

        def shutdown(self) -> None:
            delegate.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return delegate.force_flush(timeout_millis)

    return PrivateExporter()
