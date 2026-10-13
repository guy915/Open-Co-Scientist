# Monitoring

Railway's healthcheck runs only while a deploy goes live; after that only
the monitors below poll the service. This guide lists what exists and where
the code hooks in.

## Uptime

Use UptimeRobot with a five-minute interval and web/app notifications only.
Remove email contacts/actions and disable personal email subscriptions.
Keep the Azure budget email alert as the only email alert. Railway's deploy
healthcheck does not replace these recurring monitors.

| Monitor | URL | Alert when |
|---|---|---|
| API up | `https://api.open-coscientist.com/health` | status is not 200 |
| API healthy | same URL, keyword check | `"status":"healthy"` is absent |
| Site up | `https://open-coscientist.com/` | status is not 200 |

`/health` returns 503 only when the store is unreachable. A stuck run, a
terminally failed task or low disk report `"status":"degraded"` with 200, so
the keyword monitor is what catches them. The public response hides check
detail, so it is safe to poll. Do not monitor the MCP service: it is private
to Railway's network.

For all three monitors: interval **300 seconds**, timeout **30 seconds**,
notification threshold **0 minutes**, recurrence **0**, and recovery
notifications enabled. HTTP monitors expect **200**. The healthy keyword
monitor alerts when the exact keyword `"status":"healthy"` is absent;
inspect the deployed API's actual body before enabling that assertion.
Use only the operator's configured app/push/web contact. Free-plan threshold and
recurrence are fixed at zero ([UptimeRobot API](https://uptimerobot.com/api/v2/));
API alert-contact encoding is `<contact-id>_0_0`. Immediate notification means
after UptimeRobot confirms the failure, not seconds-level detection on a
five-minute polling plan.

## Error tracking

Use Sentry's free tier (or self-hosted GlitchTip, which speaks the same
protocol) with one project for the API and one for the frontend.

| Where | Variable | Value |
|---|---|---|
| Railway, api | `SENTRY_DSN` | the API project's DSN |
| Railway, api | `SENTRY_ENVIRONMENT` | `production` |
| Frontend host, build environment | `VITE_SENTRY_DSN` | the frontend project's DSN |

Both SDKs stay off while their DSN is unset, so local runs, CI and forks
send nothing. Researcher data must not leave through error reports.

The frontend reads `VITE_SENTRY_DSN` at build time
(`src/shared/lib/error_tracking.ts`, loaded as its own chunk only when built
with a DSN), so whichever host builds the frontend needs it set there; a
host change that omits it silently disables browser reporting. Frontend
reports carry no PII or console breadcrumbs, and the browser's client ID and
saved provider keys are redacted. Source-map upload is not implemented.

The API (`platform/telemetry/error_tracking.py`) reports unhandled exceptions and
`ERROR` log records. Each report:
- drops request headers (including `X-Client-ID`), cookies, bodies and query
  strings;
- has no stack-frame variables, personal data or performance traces;
- replaces the run's own provider key and every environment value whose name
  ends in `KEY`, `SECRET`, `TOKEN`, `PASSWORD` or `DSN` with `[REDACTED]`.

Only the Starlette and FastAPI integrations are on; Sentry's auto-enabled AI
and HTTP-client integrations would attach prompts and model output.

## Tracing

OpenTelemetry spans ([ADR-005](adr/005-tracing.md)) are exported only when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set and `OTEL_SDK_DISABLED` is not `true`;
otherwise no exporter, thread or socket exists. Use any OTLP/HTTP backend
with a free tier (Grafana Cloud Traces, Honeycomb or similar).

| Where | Variable | Value |
|---|---|---|
| Railway, api | `OTEL_EXPORTER_OTLP_ENDPOINT` | the backend's OTLP/HTTP endpoint |
| Railway, api | `OTEL_EXPORTER_OTLP_HEADERS` | its auth header, e.g. `Authorization=Basic ...` |
| Railway, api | `OTEL_SERVICE_NAME` | `co-scientist-api` |
| Railway, api (optional) | `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `parentbased_traceidratio`, `1.0` for count/token triggers |

The privacy projection at the exporter accepts only fixed classifications,
UUID task/run references and numeric usage. The exported fields below are the
alert contract; do not filter on internal fields that the exporter drops.

| Exported span | Useful exported fields |
|---|---|
| `HTTP GET`, `HTTP POST`, etc. | `http.request.method`, `http.response.status_code` |
| `task.execute` | `co_scientist.run_id`, `co_scientist.task.id`, `co_scientist.task.attempt`, `co_scientist.task.outcome` |
| `llm.call_llm`, `llm.call_llm_json` | fixed span name; no prompt/model identifier; tool-loop calls export as `operation` |
| `llm.attempt` | `co_scientist.llm.attempt`, `co_scientist.llm.retry_reason` |
| `llm.request` | `gen_ai.request.max_tokens`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `co_scientist.llm.reasoning_tokens`, `co_scientist.llm.cached_prompt_tokens` |

All exported resources use `service.name = co-scientist-api`. Failure spans
carry an allowlisted `error.type` or `Exception`; status text, events, prompt
names, custom/served model IDs, routes, node/task types and arbitrary resource
fields are removed. No prompt, completion, tool argument, goal, query string,
header or body is exported. Stdout has trace IDs while spans are active;
persisted logs remain local.

## Alert settings

These are operator configuration instructions; repository changes do not
create dashboards or alert recipients. Use the operator's configured webhook/app
integration for every action, remove email actions/contacts and disable
personal issue-email subscriptions. Only the Azure budget email stays
enabled.

### Honeycomb

Select the production `co-scientist-api` dataset. Each trigger uses a
**5-minute** query window, evaluates every **5 minutes**, and uses alert type
**on_change**: notify on entering triggered and resolved states, with no
repeated notifications while unchanged. Filter all queries on
`service.name = co-scientist-api`.

| Trigger | Additional filters | Calculation and threshold |
|---|---|---|
| API 5xx | `http.response.status_code >= 500` | `COUNT > 5` |
| Durable task failure | `name = task.execute`, `co_scientist.task.outcome = failed` | `COUNT > 0` |
| Provider throttles | `name = llm.attempt`, `co_scientist.llm.retry_reason = RateLimitError` | `COUNT > 5` |
| Provider connection failures | `name = llm.request`, `error.type = APIConnectionError` | `COUNT > 3` |
| Provider output burn | `name = llm.request` | `SUM(gen_ai.usage.output_tokens) > 250000` |

For these absolute count/token triggers, use sampling **1.0**;
sampled counts understate usage. The throttle trigger counts classified
retries, not every upstream 429. Output burn is a spike warning to inspect
Azure Cost Management and durable provider-usage/reservation records, not a EUR cost
estimate or credit-exhaustion detector. Missing usage is unknown, never zero.
UptimeRobot supplies the outage signal when the API cannot export spans.
Do not add researcher content to telemetry to make a query possible.

### Sentry

Use separate API and frontend projects, filtered to environment
**production**, with error/fatal levels only. In both projects create:

- **New error:** “A new issue is created”, with the operator's app/web action.
- **Regressed error:** “The issue changes state from resolved to unresolved”,
  with the same action.

These issue rules evaluate incoming events. Set per-issue action frequency
**30 minutes**. In the API project add **Error burst**: “The issue is seen
more than 10 times in 5 minutes”, with the same environment/level filters,
app/web action and **30-minute** action frequency. This detects a per-issue
burst; it does not require a separate aggregate metric-alert subscription.
Remove every “Send a notification via email” action. Keep the existing
redaction boundaries. Source maps are absent, so retain release metadata
and reproduce browser failures offline.

The optional `/webhooks/sentry/alerts` bridge writes fixed incident markers to
Honeycomb. Configure `SENTRY_ALERT_ORGANIZATION` with the organization's slug,
`SENTRY_ALERT_CLIENT_SECRET` with the webhook signing secret, and
`HONEYCOMB_MARKER_API_KEY` with the marker credential. The bridge is disabled
until all three are set. Event URLs must use HTTPS on `sentry.io` or
`<organization>.sentry.io` and match the configured organization and fixed
project/event path exactly. SDK error reporting remains controlled by the DSNs.

### Backup verification

The backup supervisor emits the fixed API error “Backup verification failed;
new research paused” through the existing Sentry privacy projection when
configured. The new/regressed API issue rules above cover it; no additional
email action is needed. The authenticated [operator view](LAUNCH-CONTROL.md)
shows whether replication is configured, verification status and the last
verified timestamp. Missing metadata is unknown, not a successful backup.
An unexpected replication-daemon exit stops the API and reaches the uptime
monitors. Follow the [backup recovery playbook](INCIDENTS.md#backup-verification-failed-or-stale)
and [idle refresh drill](RESTORE-DRILL.md#daily-idle-backup-proof) before resuming.
