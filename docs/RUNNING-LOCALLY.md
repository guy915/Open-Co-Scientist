# Running locally

See the [configuration reference](CONFIGURATION.md) for environment controls
and defaults beyond the sample files.

## Prerequisites

- Python 3.12 for the complete application and MCP service. The internal
  engine package requires Python 3.12+.
- Node.js 22.13+ for frontend tooling and Bun 1.3.14 for the committed locks.
  Install Bun from [the official instructions](https://bun.sh/docs/installation).
  To install that exact version, run
  `curl -fsSL https://bun.sh/install | bash -s "bun-v1.3.14"`.
- Provider credentials for real model responses. Missing keys return a
  no-model error. The browser harness uses an explicit deterministic test double.
- Optional Tesseract for image OCR and scanned-document extraction.
  The production API image includes it.

## Start a checkout

From the repository root:

```bash
make setup
make start
```

`make setup` installs the local engine into `.venv`, installs
frontend dependencies from `bun.lock`, copies `.env.example` when `.env` is
missing, and links `app/.env` to that root file. Existing `.env` files are
preserved. Edit the root file for API settings. The MCP service uses the
separate `engine/mcp_server/.env`; its provider keys do not come from the
API's environment file.

`make start` installs missing dependencies, frees the three development
ports, launches the services, and opens the workbench once it is ready.
It stops existing listeners on those ports; reserve them for this checkout.

| Service | Address | Notes |
| --- | --- | --- |
| Workbench | http://localhost:5173 | Vite proxies API requests to port 8008 |
| API | http://localhost:8008 | Health at `/health` |
| MCP | http://localhost:8888 | Literature/database tools; Python 3.12 required |

Use `make stop` to stop the development services. Run individual services
with `make dev-api`, `make dev-ui`, and `make dev-mcp` in separate terminals.
The API uses hot reload; an edit can interrupt an active task. For a stable
backend during a run:

```bash
cd app
COSCIENTIST_DB_PATH=../coscientist.db ../.venv/bin/python -m uvicorn co_scientist.main:app --host 127.0.0.1 --port 8008
```

Do not expose the development stack publicly. See [launch guidance](LAUNCH.md)
and [DEPLOYMENT.md](DEPLOYMENT.md) for authenticated hosting.

## Model and retrieval configuration

The default free route is `openrouter/inclusionai/ling-3.1-flash` with
`OPENROUTER_API_KEY`. Operator Express calls use free OpenRouter, then the direct
subscriber API credit, then Azure. Standard and higher tiers require BYOK.
See [credit setup](azure-setup.md). Inspect `/status` for retrieval availability.

Missing keys never enable synthetic product answers. The retired
`COSCIENTIST_FORCE_OFFLINE` flag has no effect. For a private test only, set
`COSCIENTIST_TEST_DOUBLE=deterministic` before starting the process. Never set it
on a public service. `make e2e`, container smoke and the restore drill select it
explicitly. MCP retrieval is independent; the browser harness also selects
an offline evidence resolver and intercepts its sources.

MCP configuration is documented in
[`engine/mcp_server/.env.example`](../engine/mcp_server/.env.example).
PubMed can retrieve anonymously; `ENTREZ_EMAIL` is an optional courtesy
identifier and a valid API key can raise source rate limits. Live literature
availability is determined by reachability, not the presence of an email.

## Validate changes

```bash
make check         # lint, types, suites, evaluations, smoke, build, browser tests
make docker-build  # production image builds; no deployment
```

`make test-all` runs the engine, app and MCP pytest suites, the frontend
Vitest suite and the evaluation harness tests. Each also runs alone:
`make test-engine`, `make test-app`, `make test-mcp`, `make test-frontend` and
`make test-evaluations`. `make test-sandbox-linux` runs the sandbox tests in a
Linux container with Docker, and `make audit-deps` checks the runtime locks
against online advisories (needs uv).
`make e2e` runs Playwright with a fresh temporary SQLite store, offline model
responses, API port 8108, and UI port 5273. `make e2e-production` builds the
frontend and serves its bundled assets.
It checks deep links, report retrieval after a reload, and ownership isolation.
Both targets typecheck the browser harness, disable local dotenv loading,
and explicitly select offline evidence/claim checks. Chromium is normally
installed
through Playwright. To use an already installed browser when downloads are
unavailable:

```bash
COSCI_E2E_CHROMIUM_EXECUTABLE=/usr/bin/chromium make e2e
```

That override can differ from the Chromium revision used in CI. CI uses the
Playwright-managed browser. Port overrides are `COSCI_E2E_API_PORT` and
`COSCI_E2E_UI_PORT`. Run the targets sequentially when using the same ports.
The production browser target builds into its temporary state directory and
leaves the normal frontend `dist/` artifact untouched. Vite preview tests built
assets and browser flows, not the hosting platform's routing and
configuration.

## Worktrees

Each worktree needs its own dependencies and environment because Git does not
copy ignored `.env`, `.venv`, databases, or `node_modules` directories.
Run `make setup` inside the worktree. Copy an existing `.env` only when you
intend to reuse those credentials. Keep its SQLite store separate.
Do not reuse an editable engine install from another checkout: it imports
that checkout's source and can make a test exercise the wrong branch.
Only one development stack can use the default ports at a time.

## Local data

The root development database is `coscientist.db`; reports and caches are
ignored. Deleting these is separate from dependency cleanup. Before a reset,
make a backup using [the backup procedure](LAUNCH.md#backup-and-restore).
`make reset-db` deletes the local store, and `make clean` removes development
virtual environments, frontend dependencies/build output, and local caches.
