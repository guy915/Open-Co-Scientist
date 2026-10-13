# Configuration reference

Process environment values override the working directory's `.env` for API
settings. `make setup` links `app/.env` to the root file; the MCP server uses
its own `engine/mcp_server/.env`. `PYTHON_DOTENV_DISABLED=1` disables dotenv
loading for hermetic commands. Restart the relevant process after changing
configuration; frontend `VITE_*` values are baked into its build. Never put
backend credentials in `VITE_*` variables or commit populated env files.

The sample files are starting points, not the complete control list. The
following API defaults come from [Settings](../engine/src/co_scientist/core/config.py).
Their environment names are case insensitive. Byte limits are bytes, daily
budgets use UTC days, and positive budget fields reject zero; see
[admission and storage behavior](OPERATIONS.md).
Production hosting can override these defaults; see [Deployment](DEPLOYMENT.md).

## Call and admission budgets

App operations and research share funded provider admission; BYOK still
shares compute admission. Raising a quota does not change a run tier.

| Environment name | Default |
| --- | --- |
| `APP_LLM_MAX_CALLS` | `4` |
| `APP_LLM_CLIENT_CALLS_PER_DAY` | `64` |
| `APP_LLM_GLOBAL_CALLS_PER_DAY` | `1024` |
| `APP_LLM_CLIENT_TOKENS_PER_DAY` | `2000000` |
| `APP_LLM_GLOBAL_TOKENS_PER_DAY` | `32000000` |
| `APP_LLM_MAX_OUTPUT_TOKENS` | `32768` |
| `PROVIDER_CLIENT_CALLS_PER_DAY` | `512` |
| `PROVIDER_CLIENT_TOKENS_PER_DAY` | `16000000` |
| `PROVIDER_HOST_CALLS_PER_DAY` | `512` |
| `PROVIDER_HOST_TOKENS_PER_DAY` | `16000000` |
| `PROVIDER_MAX_OUTPUT_TOKENS` | `131072` |
| `ANONYMOUS_SESSIONS_PER_HOST_PER_DAY` | `16` |
| `ANONYMOUS_SESSIONS_PER_DAY` | `1000` |
| `RUNS_PER_HOST_PER_DAY` | `32` |
| `RUNS_PER_DAY` | `200` |
| `ANONYMOUS_WRITE_GLOBAL_REQUESTS_PER_DAY` | `8192` |
| `ANONYMOUS_WRITE_CLIENT_REQUESTS_PER_DAY` | `512` |
| `ANONYMOUS_WRITE_HOST_REQUESTS_PER_DAY` | `1024` |
| `FREE_RUNS_PER_HOST_PER_DAY` | `6` |
| `FREE_RUNS_GLOBALLY_PER_DAY` | `20` |
| `CONCURRENT_RUNS_PER_HOST` | `10` |
| `CONTINUATIONS_PER_RUN_PER_DAY` | `3` |
| `CONTINUATIONS_PER_CLIENT_PER_DAY` | `8` |
| `CONTINUATIONS_PER_HOST_PER_DAY` | `16` |
| `CONTINUATIONS_PER_DAY` | `100` |
| `FREE_RUNS_PER_DAY` | `3` |

## Storage budgets

These bound anonymous writes, staged documents, messages and human
corpora across each named scope. Admission history expires with retention.

| Environment name | Default |
| --- | --- |
| `APP_LLM_MAX_INPUT_BYTES` | `256000` |
| `PROVIDER_MAX_INPUT_BYTES` | `1000000` |
| `ANONYMOUS_WRITE_GLOBAL_BYTES_PER_DAY` | `268435456` |
| `ANONYMOUS_WRITE_CLIENT_BYTES_PER_DAY` | `33554432` |
| `ANONYMOUS_WRITE_HOST_BYTES_PER_DAY` | `67108864` |
| `ANONYMOUS_STORE_MAX_BYTES` | `536870912` |
| `STAGED_DOCUMENT_GLOBAL_BYTES` | `209715200` |
| `STAGED_DOCUMENT_CLIENT_BYTES` | `26214400` |
| `STAGED_DOCUMENT_HOST_BYTES` | `52428800` |
| `USER_MESSAGE_RUN_BYTES` | `4194304` |
| `USER_MESSAGE_CLIENT_BYTES` | `16777216` |
| `USER_MESSAGE_HOST_BYTES` | `33554432` |
| `USER_MESSAGE_GLOBAL_BYTES` | `67108864` |
| `HUMAN_CORPUS_RUN_BYTES` | `26214400` |
| `HUMAN_CORPUS_CLIENT_BYTES` | `52428800` |
| `HUMAN_CORPUS_HOST_BYTES` | `104857600` |
| `HUMAN_CORPUS_GLOBAL_BYTES` | `209715200` |
| `HEALTH_CHECK_MIN_FREE_DISK_BYTES` | `104857600` |

## Models and safety

The default role model is `openrouter/inclusionai/ling-3.1-flash`. An unset
claim verifier inherits the worker model; empty chat/supervisor overrides
fall back to `MODEL_NAME`. See [LLM roles](llm-roles.md). Safety hard blocks
remain active regardless of semantic assessment settings.

Free admission checks current OpenRouter prices before dispatch. If a declared
free route becomes paid or unavailable, it selects the first verified zero-cost
route in that profile's fallback chain. The configured model can therefore
differ from the selected request model; completion telemetry records the latter.

| Environment name | Default |
| --- | --- |
| `MODEL_NAME` | `default role model` |
| `SUPERVISOR_MODEL_NAME` | `default role model` |
| `CHAT_MODEL_NAME` | `default role model` |
| `ELO_K_FACTOR` | `24` |
| `SEMANTIC_SAFETY_ENABLED` | `true` |
| `SEMANTIC_SAFETY_MODEL` | `default role model` |
| `CLAIM_VERIFIER_MODEL` | `unset` |
| `EVIDENCE_RESOLVER` | `live` |

## Serving and operations

Makefile and container entrypoints pass explicit bind ports. Keep the API
at one replica; worker width is independent of replica count.

| Environment name | Default |
| --- | --- |
| `HOST` | `0.0.0.0` |
| `PORT` | `8008` |
| `MCP_SERVER_URL` | `http://localhost:8888/mcp` |
| `MAX_CONCURRENT_RUNS` | `10` |
| `WORKER_POOL_SIZE` | `8` |
| `STATUS_PROBE_TIMEOUT_SECONDS` | `3.0` |
| `STATUS_PROBE_CACHE_TTL_SECONDS` | `30.0` |
| `HEALTH_CHECK_CACHE_TTL_SECONDS` | `5.0` |

## Telemetry and credentials

Empty credentials disable their integrations. `LOGS_ADMIN_TOKEN` grants
operator access; `BYOK_ENCRYPTION_KEY` protects stored user credentials.
`SENTRY_ALERT_CLIENT_SECRET` authenticates alert hooks;
`SENTRY_ALERT_ORGANIZATION` selects the organization whose event URLs are accepted.
Incident markers also require `HONEYCOMB_MARKER_API_KEY`; all three must be set. See
[Monitoring](MONITORING.md).

| Environment name | Default |
| --- | --- |
| `LOG_CAPTURE_ENABLED` | `true` |
| `LOG_CAPTURE_LEVEL` | `INFO` |
| `LOG_CAPTURE_MAX_ROWS` | `20000` |
| `LOGS_ADMIN_TOKEN` | `empty` |
| `LOGS_INGEST_PER_MINUTE` | `120` |
| `BYOK_ENCRYPTION_KEY` | `empty` |
| `SENTRY_DSN` | `empty` |
| `SENTRY_ENVIRONMENT` | `empty` |
| `SENTRY_ALERT_ORGANIZATION` | `empty` |
| `SENTRY_ALERT_CLIENT_SECRET` | `empty` |
| `HONEYCOMB_MARKER_API_KEY` | `empty` |

## Direct environment readers

These controls are read outside Settings. They need process environment or
the appropriate dotenv loader; they are not frontend build variables.

| Controls | Meaning and source |
| --- | --- |
| `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Provider credentials; Gemini accepts either name. Azure requires `AZURE_OPENAI_API_KEY`. |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_KEY` | Azure resource HTTPS origin, Luna deployment and credential. The transport always uses `/openai/v1/`; see [Azure setup](azure-setup.md). |
| `LLM_ENABLED`, `LLM_AZURE_ENABLED`, `LLM_TOTAL_BUDGET_EUR`, `LLM_USD_TO_EUR`, `LLM_AZURE_EXPIRES_AT`, `LLM_AZURE_CUTOFF_HOURS` | Global model admission (enabled by default) and Azure admission (disabled by default). Azure needs a positive budget, exchange rate and UTC expiry; cutoff defaults to 48 hours. See [spending cutoff](azure-setup.md#spending-cutoff). |
| `LLM_OPENROUTER_CALLS_PER_DAY`, `ANTHROPIC_MONTHLY_CREDIT_USD`, `ANTHROPIC_BILLING_RESET_DAY` | Operator-funded route allowances; defaults 1000 calls, USD 100 and day 7. See [Anthropic credit](anthropic-credit.md) and [Azure setup](azure-setup.md). |
| `LLM_EFFORT_<ROLE>`, `LLM_WORKER_EFFORT`, `LLM_SUPERVISOR_EFFORT` | Role effort override wins over tier effort. Accepted efforts and role defaults are in [LLM roles](llm-roles.md). |
| `COSCIENTIST_REQUIRE_FREE_MODELS` | Defaults off; restricts admissible models to the free catalog when enabled. |
| `COSCIENTIST_LLM_TIMEOUT_SECONDS`, `COSCIENTIST_MCP_TOOL_TIMEOUT_SECONDS` | Bound provider calls and MCP tools; defaults and thinking-floor rules are in [Operations](OPERATIONS.md). |
| `MCP_SERVER_URL`, `COSCIENTIST_MCP_SHARED_SECRET` | MCP endpoint (default `http://localhost:8888/mcp`) and matching client/server secret; an empty endpoint disables retrieval. |
| `COSCIENTIST_DB_PATH`, `COSCIENTIST_TRUSTED_PROXY_CIDRS`, `ALLOWED_ORIGINS`, `SSE_MAX_CONNECTIONS` | SQLite path (default `./coscientist.db`), trusted proxies, browser origins and SSE cap (default 256). See [Trusted proxy](TRUSTED-PROXY.md) and [Deployment](DEPLOYMENT.md). |
| `COSCIENTIST_RUN_RETENTION_DAYS`, `COSCIENTIST_DRAFT_RETENTION_DAYS`, `COSCIENTIST_DOCUMENT_RETENTION_DAYS` | Expiration defaults: 90, 7 and 30 days. Maintenance ignores invalid/nonpositive values; see [Operations](OPERATIONS.md). |
| `COSCIENTIST_WORKSPACE_DIR` | Stable run workspaces; defaults to `coscientist-workspaces` under the system temporary directory. Keep workspaces off the database volume. |
| `COSCIENTIST_SKILLS_DIR`, `COSCIENTIST_SKILLS_PYTHON`, `COSCIENTIST_CGROUP_ROOT` | Skill bundle/interpreter and Linux delegated cgroup root; see [Engine guidance](../engine/AGENTS.md) for confinement requirements. |
| `NCBI_API_KEY` / `ENTREZ_API_KEY`, `USER_EMAIL` / `ENTREZ_EMAIL`, `NCBI_TOOL`, `ALPHAGENOME_API_KEY`, `OPENALEX_API_KEY`, `FDA_API_KEY` | Skill-scoped optional credentials/contact information. The skill-facing name wins over its host alias; only the selected skill's credentials reach its subprocess. |
| `FORCE_LITERATURE_REVIEW`, `COSCIENTIST_DEV_MODE` | Set the first to `0` to disable forced literature review. Dev mode defaults false and reduces the literature-review budget; explicit per-run options win. |
| `COSCIENTIST_TEST_DOUBLE` | Exact value `deterministic` selects the hermetic test backend; gates strip real credentials. See [CI](CI.md). |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TRACES_HEADERS`, `OTEL_SERVICE_NAME`, `OTEL_SDK_DISABLED` | Optional trace export, headers and service name; `OTEL_SDK_DISABLED=true` suppresses export. See [Monitoring](MONITORING.md). |
| `LITESTREAM_R2_BUCKET`, `LITESTREAM_R2_ENDPOINT`, `LITESTREAM_R2_ACCESS_KEY_ID`, `LITESTREAM_R2_SECRET_ACCESS_KEY` | Backup replica configuration; all four are required. See [Deployment](DEPLOYMENT.md) and [Restore drill](RESTORE-DRILL.md). `COSCIENTIST_LITESTREAM_ACTIVE=1` is set by the backup supervisor, not an independent backup switch. |

Completion email has no sender or SMTP settings. Legacy durable notification
tasks settle as disabled. `AZURE_API_KEY` and `AZURE_OPENAI_API_VERSION` no
longer configure Azure. `LLM_AZURE_UNTIL` is retired and ignored; use the
exact expiry instead. `COSCIENTIST_FORCE_OFFLINE` is ignored by the engine/API
and survives only as a live-evaluation refusal guard.

## MCP service

| Controls | Meaning |
| --- | --- |
| `COSCIENTIST_MCP_PORT`, `COSCIENTIST_MCP_LOG_LEVEL`, `LOG_LEVEL` | Bind port (8888) and log level (`INFO`); MCP-specific level takes precedence over `LOG_LEVEL`. |
| `COSCIENTIST_MCP_SHARED_SECRET`, `COSCIENTIST_MCP_ALLOW_UNAUTHENTICATED_LOCAL` | Shared secret is required. The local bypass must be exactly `1` and only permits actual loopback peers; never enable in production. |
| `ENTREZ_EMAIL`, `ENTREZ_API_KEY`, `OPENALEX_MAILTO`, `OPENALEX_API_KEY` | Retrieval credentials/contact; Entrez email wins over OpenAlex mailto. |
| `WEB_SEARCH_PROVIDER`, `BRAVE_API_KEY`, `TAVILY_API_KEY` | Optional preferred search provider and its credentials; a refusal/exhaustion can fall through to the other configured provider. |
| `COSCIENTIST_LIT_REVIEW_DIR` | Literature cache; default `./cache/literature_review`. Keep it off the database volume. |
| `DISABLE_SSL_VERIFY` | A refusal guard: TLS bypass requests fail rather than disabling certificate checks. |

See the [MCP README](../engine/mcp_server/README.md) for service setup.

## Frontend and local tooling

| Controls | Meaning and reference |
| --- | --- |
| `VITE_API_BASE_URL`, `VITE_SENTRY_DSN`, `VITE_SENTRY_ENVIRONMENT`, `COSCI_FRONTEND_DIST` | Build-time API/error-tracking configuration and optional output directory; see [Frontend](../app/frontend/README.md) and [Monitoring](MONITORING.md). |
| `COSCI_E2E_API_PORT`, `COSCI_E2E_UI_PORT`, `COSCI_E2E_STATE_DIR`, `COSCI_E2E_PRODUCTION`, `COSCI_E2E_BROWSER_SHARD`, `COSCI_E2E_CHROMIUM_EXECUTABLE`, `BUN` | Browser harness overrides; defaults use ports 8108/5273 and an isolated temporary database. See [E2E](../e2e/README.md). |
| `FP_API`, `FP_ORIGIN`, `FP_PORT`, `FP_ROUTES`, `FP_CHROME`, `FP_TOOLS`, `FP_TLS_CERT`, `FP_TLS_KEY` | Local performance preview/measurement; default preview is 5273. See [Performance workflow](../app/frontend/scripts/performance/README.md). |
| `FP_DEVICES`, `FP_SKIP_AUDITS` | Lighthouse devices default to `mobile,desktop`; a nonempty skip flag disables Lighthouse audits while keeping browser measurements. |
| `CLAIM_SUPPORT_KEEP_DB` | Preserve a claim-support evaluation's temporary database for inspection. See [Evaluations](../evaluations/README.md). |
| `LOAD_TEST`, `LOAD_FIXTURE_PAGES`, `LOAD_DURATION`, `LOAD_MODEL_DELAY`, `LOAD_PEERS`, `LOAD_PREFLIGHT`, `SSE_PAGES`, `START_ATTEMPTS`, `VISITORS_PER_MINUTE` | Isolated load harness only. `LOAD_TEST=1` also requires its `/app/data/load.db` volume; fixture pages default to 200. See [Load evaluation](../evaluations/load/README.md). |
| `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_BASE` | Live benchmark refusal guards: custom provider endpoints are rejected. They do not configure the application transport. |
| `LITELLM_LOCAL_MODEL_COST_MAP`, `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS` | Hermetic evaluation setup suppresses model metadata downloads; see [Evaluation harness](../evaluations/README.md). |
| `ACTIONLINT_BIN`, `GITLEAKS_BIN`, `ZIZMOR_BIN`, `BASE_SHA`, `HEAD_SHA`, `PR_TITLE`, `PR_BODY`, `RESULTS` | Local CI checker inputs/tool overrides; see [CI](CI.md) and [checker sources](../scripts/ci/). |

`PATH`, `CI`, `GITHUB_EVENT_NAME`, `GITHUB_EVENT_PATH`, `GITHUB_OUTPUT`,
`GITHUB_REPOSITORY`, `GITHUB_TOKEN` and Vite's `MODE` come from the shell,
CI runner or build tool. `RUNNER_RECORD` is a runner-test fixture hook.
They are not API deployment settings.
