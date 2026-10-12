# Deployment (production hosting)

See the [configuration reference](CONFIGURATION.md) for environment controls
and defaults beyond the sample files.

How the deployed services are built and configured. The load-bearing
invariants are also summarised in the [root AGENTS.md](../AGENTS.md)
Production hosting section; keep the two in sync when either changes.

[LAUNCH.md](LAUNCH.md) covers release validation, backup and restore, and
repository publication. Deployment settings live in the hosting dashboards,
not in this repository; read the intended environment back before relying on
any value below.

## Services

| Layer | Platform | URL |
|---|---|---|
| Frontend (Vite/React) | Cloudflare Worker `open-coscientist` | https://open-coscientist.com |
| API (FastAPI) | Railway service `api` | https://api.open-coscientist.com |
| MCP server | Railway service `mcp` | private network only, port 8888 |

The frontend uses `wrangler.jsonc`. The API and MCP run in one Railway
project with exactly one API replica. Production starts with an empty database.

The worker runs embedded in the API process; there is no separate worker
service. The single-writer SQLite store bounds worker width
(`worker_pool_size`, see `app/AGENTS.md`).

## Images

Both Railway services build from the repository's root Dockerfiles
(`Dockerfile.api`, `Dockerfile.mcp`). They install the hash-pinned runtime
closures in [`requirements/`](../requirements/README.md) before installing
local source without dependency resolution, and pin the base image by digest.
Build contexts exclude local secrets, databases, session notes and generated
artifacts; keep those exclusions when adding an image or context.

- **API.** The entrypoint (`scripts/api-entrypoint.sh`) honors Railway's
  injected `PORT` (default 8008) and passes `--timeout-graceful-shutdown 20`
  so open SSE streams cannot hold shutdown until the platform kills the
  process; keep the Railway draining window at least that long. With the four
  `LITESTREAM_R2_*` credentials set it runs the API under the backup
  supervisor and Litestream (below); without them it serves directly. The image has a `HEALTHCHECK` on `/health`, installs
  `tesseract-ocr` (image and PDF-figure OCR; uploads fail without it, and the
  test suite fakes the dependency), and defaults `COSCIENTIST_DB_PATH` to
  `/app/data/coscientist.db`, the persistent volume mount. It serves
  `co_scientist.serving:create_app`, which reads Railway's `X-Real-IP` only
  from peers in `COSCIENTIST_TRUSTED_PROXY_CIDRS`; without that variable, the
  socket peer is the host bucket ([trusted visitor addresses](TRUSTED-PROXY.md)).
- **MCP.** Deliberately ignores `PORT` and pins 8888, binding `--host ::` for
  Railway's IPv6 private network. Its healthcheck probes `[::1]:8888`, since
  a 127.0.0.1 probe can miss an IPv6-only socket. It runs as the unprivileged
  `coscientist` user.
- Railway's healthcheck gates only the deploy going live; nothing on Railway
  polls it afterwards ([MONITORING.md](MONITORING.md) covers uptime). A
  service with a volume cannot run two deployments at once, so an API deploy
  has a short gap and never overlaps two writers.

### Railway source and watch paths

Both services use `guy915/Open-Co-Scientist`, branch `main`, build root `/`,
and their root Dockerfile, with no start-command override. Use the MCP
service's private DNS hostname in the API's `MCP_SERVER_URL`.
Set the API healthcheck path to `/health`, restart policy to `ON_FAILURE`
with 10 retries, and deployment draining to 30 seconds so the entrypoint's
20-second shutdown grace can finish.

Watch every Dockerfile input: an omitted path silently skips a deployment
when that input changes. Enter these paths separately in each service's
Build settings; keep them aligned with its `COPY` instructions.

| API | MCP |
| --- | --- |
| `/Dockerfile.api` | `/Dockerfile.mcp` |
| `/.dockerignore` | `/.dockerignore` |
| `/requirements/api.txt` | `/requirements/mcp.txt` |
| `/requirements/skills.txt` | `/engine/mcp_server/**` |
| `/litestream.yml` | `/LICENSE` |
| `/scripts/api-entrypoint.sh` | `/NOTICE` |
| `/LICENSE` | |
| `/NOTICE` | |
| `/engine/pyproject.toml` | |
| `/engine/README.md` | |
| `/engine/LICENSE` | |
| `/engine/NOTICE` | |
| `/engine/src/**` | |
| `/vendor/science-skills/**` | |

### Invariants: do not clean these up

**`RAILWAY_RUN_UID=0` must stay set on the API.** Both images create a
non-root `coscientist` user, but Railway mounts the volume over `/app/data` at
runtime as a root-owned filesystem that the image's `chown` cannot reach. An
unprivileged API cannot write SQLite, dies in its lifespan hook before
binding a port, and every deploy fails its healthcheck regardless of content.
Making the API genuinely unprivileged needs a root entrypoint that chowns the
mounted volume and then drops privileges.

**The API runs at exactly one replica.** The store is SQLite in WAL mode with
`synchronous=NORMAL` (`platform/db/__init__.py`). That pairing never corrupts
the database but can lose the last few commits on an OS crash, a trade that
is only sound single-writer. Nothing in the code refuses a second replica,
and a second process writing the same file is unserialized concurrent
writes that WAL locking does not make safe across volume backends. Growing
past one replica is a store migration (Postgres or similar), not a tuning
knob. See [OPERATIONS.md](OPERATIONS.md).

## Frontend

Cloudflare builds from repository root `/`, branch `main`, with
`NODE_VERSION=24.19.0` and `BUN_VERSION=1.3.14`. Set the build command to
`cd app/frontend && bun install --frozen-lockfile && bun run build`
(`tsc`, `vite build`, prerender) and the deploy command to
`npx --yes wrangler@4.79.0 deploy` from the root. The config is
`wrangler.jsonc`, Worker name `open-coscientist`, assets directory
`app/frontend/dist`. Build-time variables (read at build, not
runtime): `VITE_API_BASE_URL=https://api.open-coscientist.com`, and
`VITE_SENTRY_DSN` when error tracking is on (see
[MONITORING.md](MONITORING.md)). Only `VITE_` variables belong on the
frontend host; everything there is compiled into public assets.

- **Cloudflare** (`wrangler.jsonc`): a Worker (`app/frontend/worker.mjs`)
  serves the `dist` assets and answers any missing path outside `/assets/`
  with `/index.html`, so deep links load the app while a missing hashed asset
  stays a 404 instead of returning HTML. `app/frontend/public/_headers` sets
  the headers below. CI checks the config and runs `worker.test.mjs`.
- Headers: `/assets/*` is cached for a year and immutable; every path sends
  `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: strict-origin-when-cross-origin`,
  `X-Frame-Options: DENY`, a `Permissions-Policy` that denies camera,
  microphone, geolocation and payment, and a Content-Security-Policy. `/` and
  `*.html` are `no-cache`. The policy's `connect-src` names the API origin:
  a new API domain needs a `_headers` change as well as `VITE_API_BASE_URL`.

## Database replication (optional)

Setting `LITESTREAM_R2_BUCKET`, `LITESTREAM_R2_ENDPOINT`,
`LITESTREAM_R2_ACCESS_KEY_ID` and `LITESTREAM_R2_SECRET_ACCESS_KEY` on the
API (optionally `LITESTREAM_R2_PATH`, default `api/coscientist`) turns on
continuous replication of the SQLite file to Cloudflare R2 (`litestream.yml`).
On start the entrypoint restores the database from the replica only when the
volume has none, and fails closed on a network or corrupt-backup error rather
than starting empty. While Litestream runs, the API disables SQLite's
automatic and shutdown checkpoints and the config keeps the WAL high-water
mark (`truncate-page-n: 0`), because Litestream owns checkpointing and a
truncating checkpoint can stall serving writers. Replication does not replace
tested restores ([backup and restore](LAUNCH.md#backup-and-restore)).

The entrypoint starts the API under `platform/db/backup_service.py`. The
supervisor runs `litestream replicate`. When the database file exists, and
then every 24 hours, it pauses replication, forces a snapshot, restores it into a scratch file and runs `PRAGMA integrity_check`.
A failed check pauses new research ([launch control](LAUNCH-CONTROL.md)),
reports one error to Sentry when a DSN is set, and retries after one hour. A
success does not resume admission. The result is written to
`<database>.backup-status.json` beside the database. If replication stops, the
supervisor stops the API so that Railway restarts the service.

## API configuration

Non-secret routing and storage variables on the `api` service:

```
MCP_SERVER_URL=http://<mcp-private-host>:8888/mcp
COSCIENTIST_DB_PATH=/app/data/coscientist.db
RAILWAY_RUN_UID=0                                  # must stay set
COSCIENTIST_TRUSTED_PROXY_CIDRS=100.64.0.0/24      # see TRUSTED-PROXY.md
ALLOWED_ORIGINS=https://open-coscientist.com
COSCIENTIST_RUN_RETENTION_DAYS=0
```

The optional paid Azure fallback needs `LLM_AZURE_ENABLED=1`,
`LLM_TOTAL_BUDGET_EUR`, `LLM_AZURE_EXPIRES_AT`, `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT` and an operator-recorded
allowance in the store; without all of them Azure stays off
([Azure setup](azure-setup.md)).

The zero run-retention setting disables scheduled deletion of completed runs;
document retention remains separate (`COSCIENTIST_DOCUMENT_RETENTION_DAYS`).
Give each deployment its own volume and R2 prefix so a restore cannot import
another deployment's research data. Keep `LITESTREAM_R2_*`, provider, Sentry
and OTLP settings on the services that use them.

API error tracking uses `SENTRY_DSN` and `SENTRY_ENVIRONMENT`; tracing uses
`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` and
`OTEL_SERVICE_NAME` ([MONITORING.md](MONITORING.md)). These are backend
settings, distinct from the public build-time `VITE_SENTRY_DSN`.

- **CORS.** `ALLOWED_ORIGINS` is a comma-separated allowlist and enables
  credentialed CORS. When unset, `main.py` uses `DEFAULT_ALLOWED_ORIGINS`
  (the local dev origins and the public site). Set it explicitly to an empty
  value and the API falls back to `Access-Control-Allow-Origin: *` without
  credentials, so any origin can call it from a browser.
- **Models.** The defaults are declared in
  `engine/src/co_scientist/core/config.py` and the per-model facts (routing,
  fallbacks, price) in `engine/src/co_scientist/platform/llm/profile/`.
  Production can override the four role variables (`MODEL_NAME`,
  `SUPERVISOR_MODEL_NAME`, `CHAT_MODEL_NAME`, `SEMANTIC_SAFETY_MODEL`); a
  default change reaches production only when those variables are unset or
  changed. Leave `CLAIM_VERIFIER_MODEL` unset so claim assessment inherits the
  worker model, and keep `COSCIENTIST_REQUIRE_FREE_MODELS` off so explicit
  BYOK stays available.
- **Secrets** (values never enter this repository or document): the provider
  keys (`OPENROUTER_API_KEY`, and any other provider in use), and:
  - `BYOK_ENCRYPTION_KEY`, which encrypts bring-your-own-key credentials for
    the run lifetime; BYOK is unavailable without it.
  - `LOGS_ADMIN_TOKEN`, the operator token for the app-wide persisted-log
    view, sent as `X-Logs-Token`. Without it, no caller gets that view;
    loopback callers get no operator access.
- **MCP shared secret.** `COSCIENTIST_MCP_SHARED_SECRET` must be identical on
  both services. When set, every MCP call except the plain `/` status route
  must carry it in `X-MCP-Shared-Secret` or receive 401
  (`engine/mcp_server/auth_middleware.py`); the engine client adds the header
  (`platform/retrieval/mcp_client/__init__.py`). When it is unset, MCP refuses
  every call except `/`. Only local development can opt out, with
  `COSCIENTIST_MCP_ALLOW_UNAUTHENTICATED_LOCAL=1` and a loopback peer. Read
  back its presence on both services without exposing it. MCP
  is server-to-server only and allows no browser origin.
- **MCP service variables:** `COSCIENTIST_MCP_PORT=8888`, the search keys
  (`WEB_SEARCH_PROVIDER` with `BRAVE_API_KEY` or `TAVILY_API_KEY`),
  `OPENALEX_API_KEY`, and `ENTREZ_EMAIL`/`ENTREZ_API_KEY`; see
  `engine/mcp_server/.env.example`.

### Science skills

The skills bundle is enabled on the API image and only for hypothesis
drafting. `Dockerfile.api` copies `vendor/science-skills/` and sets
`COSCIENTIST_SKILLS_DIR`; keep the COPY and the ENV together, because the
variable is the entire gate and the catalogue is empty without it.
`WorkspaceSession.skills_enabled` defaults off and only the drafting pass
sets it (the simulation reviewer measured worse with the bundle). Scripts run
under a purpose-built interpreter at `/app/skills-venv/bin/python`
(`COSCIENTIST_SKILLS_PYTHON`) built with `polite-http` and `python-dotenv`;
`uv run` cannot work because uv's cache holds a `.git` directory the sandbox
refuses. The heavy AlphaGenome and Predicting-the-Past closure is
deliberately not installed, and the catalogue withholds skills whose
dependencies or scripts are absent rather than advertising them. Script
execution also needs the cgroup boundary below. Rationale: `engine/AGENTS.md`.

### Completion email

Public completion email is disabled until recipient verification and durable
delivery admission exist. There is no SMTP sender or transport configuration.
`/status` reports `email_notifications_available=false`; legacy queued
`notification.email` tasks finish with `status=disabled` without sending mail.

### Generated commands

Generated draft programs have no network access; source retrieval uses the
guarded MCP boundary. Linux confines each command with Landlock (explicit
workspace and immutable runtime read roots), seccomp, and its own cgroup v2
with a 512 MiB memory, 64-process and one-CPU ceiling. Descendants join the
cgroup before untrusted code starts and are killed on completion, timeout,
cancellation or workspace close. Set `COSCIENTIST_CGROUP_ROOT` only to a
writable, operator-delegated cgroup v2 subtree providing `cgroup.kill`,
`memory.max`, `pids.max` and `cpu.max`. Landlock ABI 3 is required, available
in upstream Linux 6.2+; the LSM must be enabled.

Verify both capabilities:

```
python -c "from co_scientist.platform.sandbox import sandbox_backend, command_lifecycle_available; print(sandbox_backend(), command_lifecycle_available())"
```

A filesystem backend alone does not enable execution. When either capability
is missing (including macOS without a lifecycle boundary), command tools and
runnable skills are withheld and research continues without generated
programs. Do not assume Railway delegates cgroups, and do not grant the API
broad host privileges to make the probe pass.
