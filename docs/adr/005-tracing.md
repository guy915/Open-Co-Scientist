# ADR-005: Tracing

**Status:** Accepted.

## Context

Run diagnostics are persisted logs (`app_logs`), per-call model telemetry
folded into run metrics, `run_events`, and Sentry for errors
(`platform/telemetry/error_tracking.py`, off unless `SENTRY_DSN` is set). None
of them shows one request or one node as a tree of timed steps: which model
attempt, retry or tool round a slow node spent its time in has to be rebuilt
from log lines. CI and tests are hermetic: no network, no keys.

## Decision

**OpenTelemetry, with spans created by hand at three seams, exported only
when the standard exporter variable is set.** Span names and attributes are
listed in `docs/MONITORING.md`, which also covers the environment variables.

- **Dependencies:** `opentelemetry-api` for span creation; the SDK and the
  OTLP/HTTP exporter are imported only inside `configure_tracing()`. No
  auto-instrumentation packages: three seams do not justify their dependency
  trees, and hand-made spans keep content out by construction.
- **Off by default.** `platform/telemetry/tracing.py` configures a tracer
  provider (called from the `co_scientist.main` lifespan) only when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set and `OTEL_SDK_DISABLED` is not `true`.
  Otherwise a no-op tracer is used and no exporter, thread or socket exists.
- **Seams:**
  - **LLM gateway** (ADR-004, `platform/llm/telemetry.py`): one span per
    logical call (`call_llm`, `call_llm_json`, `call_llm_with_tools`), one per
    attempt of the attempt ladder, and one client span per physical provider
    request in `complete_request`. Attributes follow the OpenTelemetry GenAI
    conventions where a name exists (`gen_ai.operation.name`,
    `gen_ai.request.model`, `gen_ai.response.model`,
    `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
    `gen_ai.request.max_tokens`) and `co_scientist.llm.*` otherwise
    (reasoning and cached prompt tokens, attempt, retry reason, budget rung,
    route, prompt name, surface).
  - **Task runner:** one `task.execute` span per durable task execution in
    `orchestration/task_worker`, with run id, task id, task type, node,
    attempt and outcome (for example committed or lease_lost).
  - **HTTP:** one server span per request from `api/tracing.py`
    (`TracingMiddleware`), named by the route template, with method, status
    and route. No query strings, headers or bodies.
- **No content.** Prompts, completions, tool arguments, goal text and
  attachment text never become span attributes or events, and exception
  messages are not recorded (only the error type).
- **Existing telemetry stays.** Persisted logs, run metrics, `run_events` and
  Sentry are unchanged; spans add to them. Log records carry the current trace
  id when a span is active, so a log line leads to its trace.
- **Context across worker cohorts:** each cohort runs its own event loop;
  spans use `contextvars`, and no asyncio primitive is shared between loops.

## Consequences

- OpenTelemetry packages are runtime dependencies of the api image, reviewed
  with `make audit-deps`.
- A slow node can be read as one trace: task, logical call, attempt, request.
- Span names and attributes are an operator-facing contract; renames are
  avoided like logger-name renames.
