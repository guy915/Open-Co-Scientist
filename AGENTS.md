# AGENTS.md

Co-Scientist (public name Open Co-Scientist, https://open-coscientist.com) is an
open replication of Google's AI Co-Scientist: a multi-agent engine that
generates, reviews, ranks and evolves research hypotheses, served by a FastAPI
API and a React workbench. This file is for coding agents arriving cold. Read
it, then the guide for the code you touch; [docs/GLOSSARY.md](docs/GLOSSARY.md)
defines terms such as tier, cohort, lease, fan-out and BYOK.

- [`engine/AGENTS.md`](engine/AGENTS.md): package map, science agents, LLM
  layer, retrieval, sandbox and the MCP (Model Context Protocol) tool server.
- [`app/AGENTS.md`](app/AGENTS.md): HTTP API, durable task runtime and SQLite
  store (their Python lives in `engine/src/co_scientist/`), the backend test
  harness, the React frontend and local Docker Compose.

## Layout

| Path | Holds |
|---|---|
| `engine/` | Package `co-scientist-engine` (import `co_scientist`, source in `src/co_scientist/`): the whole server, from science agents to API. Installed editable, never published. `mcp_server/` is the MCP tool server, a separate project deployed as the `mcp` service. |
| `app/` | `frontend/` (React, Vite, Bun), `tests/` (backend suite), `docker/` and `docker-compose.yml` (local stack), `dev/` scripts. No production Python. |
| `evaluations/` | Offline evaluation gates, the manual live quality benchmark, and repository guards in `tests/`. Not installable: run `python -m evaluations.<module>` from the root ([README](evaluations/README.md)). |
| `e2e/` | Playwright suites: `tests/` (dev server), `production/` (built assets), `support/` ([README](e2e/README.md)). |
| `docs/` | Live documentation, indexed by [docs/README.md](docs/README.md): start with `ARCHITECTURE.md` and `RUNNING-LOCALLY.md`; decisions in `adr/`. |
| `scripts/` | Gate selection (`presubmit.py`, `select_targets.py`), CI guards (`ci/`), the production API entrypoint. |
| `requirements/` | Hash-pinned Python 3.12 Linux locks for the images and the reviewed license inventory ([README](requirements/README.md)). |
| `vendor/` | Third-party code shipped as-is at a pinned revision (`NOTICE`); never edit or reformat it. `science-skills/` is Google DeepMind's Science Skills bundle, copied into the API image. |

## Set up and run

Work on macOS or Linux (WSL2 on Windows) with Python 3.12, Node.js 22.13+ and
exactly Bun 1.3.14; `make check-tools` rejects any other Bun and older Node. uv
is only for `make audit-deps` and locks.

- `make setup` creates `.venv` (never rebuilt once it exists), installs the
  engine editable with dev extras and the frontend packages, copies
  `.env.example` to `.env` and links `app/.env` to it.
  `make dev-mcp` or `make test-mcp` creates the MCP server's `.venv-mcp`.
- `make start` is the single entry point: it installs missing dependencies,
  frees ports 8008, 5173 and 8888, runs MCP, API and UI and opens the browser.
  `make stop` frees the ports; `make dev-api`, `dev-ui` and `dev-mcp` run one
  service, and MCP is skipped when `python3.12` is missing. The API reads the
  root `.env`; the MCP server reads only `engine/mcp_server/.env`.

A real run needs a provider credential (`OPENROUTER_API_KEY` for the default
free route); without one, requests fail with "No model is available right now".
The gates need no key: app tests, browser suites and offline evaluation runs
strip provider keys and set `COSCIENTIST_TEST_DOUBLE=deterministic` (exact
value); engine tests install a fake backend.
`COSCIENTIST_FORCE_OFFLINE` is retired: the engine and API ignore it, though a
few evaluation scripts still treat it as a refusal guard. Default models are
declared in `engine/src/co_scientist/core/config.py`; environment overrides
win, so check a deployment's variables rather than an old snapshot.

## Gates

| Command | Runs |
|---|---|
| `make lint` | Ruff (engine with `mcp_server`, `app/tests`, evaluations, gate scripts), `make arch`, then gts on the frontend |
| `make arch` | Import contracts in `.importlinter` ([ADR-002](docs/adr/002-layering-enforcement.md)) |
| `make typecheck` | Strict mypy on app tests, engine, evaluations and gate scripts (MCP mypy runs in `make test-mcp`) |
| `make test-all` | Engine, app and MCP pytest (plus MCP strict mypy), evaluation tests, frontend Vitest; each also alone as `make test-engine`, `test-app`, `test-mcp`, `test-evaluations`, `test-frontend` |
| `make eval-smoke` | Offline safety and citation gate |
| `make e2e` | Chromium suite on an isolated stack (API 8108, Vite 5273, fresh SQLite, test double); installs its own packages and browser after `make setup` |
| `make e2e-production` | The same harness against built assets: deep links, reloads, headers, ownership isolation |
| `make build` | Production frontend build; `make build-checked` adds bundle budgets |
| `make check` | lint, typecheck, test-all, eval-smoke, build, e2e, e2e-production |
| `make presubmit` | `make ci-guards`, then the targets CI selects for your committed diff against `origin/main`; `PRESUBMIT_ARGS=--dry-run` lists them |
| `make ci-guards` | Checker tests, Markdown links, commit hygiene, secret scan (downloads Linux x64 tools) |
| `make lint-workflows`, `make test-sandbox-linux` | Workflow lint (downloads tools); Linux sandbox confinement (needs Docker) |
| `make docker-build` | Builds three images (api, mcp, dev UI) and validates the Compose config |
| `make audit-deps` | Online dependency advisories (uv); separate from the offline gates |

`make help` lists the common targets. Before pushing, merge current `main`,
commit, and run `make presubmit`; it ignores uncommitted changes. Code changes
also need `make check`; docs-only changes need `make lint` and
`make test-evaluations`; repository-wide code or configuration changes also
need `make docker-build`. Record each gate's real exit status in the
Validation section of `.github/pull_request_template.md` and link the issue
([CONTRIBUTING.md](CONTRIBUTING.md)).

**CI** ([docs/CI.md](docs/CI.md)): `ci.yml` runs on pull requests (a push or a
title or body edit starts a new run and cancels the old one) and on every push
to `main` (all but the PR-only checks and the macOS sandbox job, never
cancelled); `nightly.yml` runs the full suite daily. Jobs follow
`.github/ci_paths.json`, as in presubmit. The only required status is `Required
checks`; `.github/rulesets/main.json` allows squash merges only, requires
resolved review threads but not an up-to-date branch, so merge `main` yourself.
Tests never call live models, fetch external URLs or retry. `benchmark.yml` is
a manual live benchmark that spends money; the manual `prune-branches.yml`
deletes stale branches unless `dry_run` is set.

## Rules no tool checks

### Operational invariants

Read the rationale in [OPERATIONS](docs/OPERATIONS.md) or
[ARCHITECTURE](docs/ARCHITECTURE.md) before changing the code involved.

- Never VACUUM from the serving process. Truncating WAL checkpoints run only in
  the startup prune and at shutdown, and not at all while Litestream replicates;
  add no others.
- Never hold a SQLite write lock across network I/O, never write on every poll
  tick, and never add a table that grows per task or per LLM call.
- Keep startup cheap; run recovery executes outside the port-binding path.
- Preserve durable task idempotency, leases, retry budgets and explicit
  recovery. Node keys persist in task types, checkpoints and idempotency keys;
  rename them only with a migration.
- A run with queued tasks due later keeps its worker cohort alive. Log cursor
  IDs may have gaps; never assume they are consecutive.
- Steering is consumed on observation, so it buys one cycle beyond an exhausted
  ceiling to incorporate the input before it is marked applied.
- No asyncio primitive may be shared between the worker cohorts' event loops.
- Every provider call goes through the LLM layer, so call ceilings and spend
  admission see it. Preserve bounded calls, budget escalation, admission
  reservations and spend caps. Lower a test envelope when a change lowers a
  tier; never raise one to make a test pass.
- Evidence gates distinguish missing, unsupported, contradictory and unsafe
  findings. Hypothesis lineage is append-only, and each run records where its
  results came from.
- Source retrieval and model-written programs cross trust boundaries: keep the
  MCP URL guards, sandbox confinement, and no execution tools where
  confinement is unavailable.

### Production hosting

Keep in step with [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Hosting settings
live in the dashboards, not in this repository.

- Frontend and DNS on Cloudflare ([DEPLOYMENT](docs/DEPLOYMENT.md#frontend)): Worker
  `open-coscientist` (`wrangler.jsonc`) serves `app/frontend/dist` through
  `app/frontend/worker.mjs` at https://open-coscientist.com; headers and CSP
  are in `app/frontend/public/_headers`.
- API and MCP in one Railway project, built from `Dockerfile.api` and
  `Dockerfile.mcp` on `main`, no start-command override. The API is
  https://api.open-coscientist.com; MCP is private-network only, port 8888.
  Merging to `main` triggers the Cloudflare build and every Railway service
  whose watched paths (dashboard settings) changed.
- With `LITESTREAM_R2_BUCKET`, `_ENDPOINT`, `_ACCESS_KEY_ID` and
  `_SECRET_ACCESS_KEY` set, the API entrypoint replicates SQLite to R2 and
  restores it when the volume has no database and a replica exists; otherwise
  it serves directly.

Three facts are load-bearing:

- **The API runs at exactly one replica.** SQLite in WAL mode with
  `synchronous=NORMAL` is sound only with one writer; growing past one replica
  is a store migration, not a setting.
- **`RAILWAY_RUN_UID=0` stays set on the API.** Railway mounts a root-owned
  volume over `/app/data`; an unprivileged process cannot write the database,
  dies in its lifespan hook, and every deploy fails its healthcheck.
- **Caches stay off the volume.** `/app/data` holds only the SQLite store, its
  WAL and the backup supervisor's files (`Dockerfile.api`,
  `engine/src/co_scientist/platform/db/backup_service.py`); a full volume is an
  [incident](docs/INCIDENTS.md). Never put caches or outputs there.

### Trust boundaries

- `main` is protected; change it only through a pull request. Only the
  deployment owner changes production: https://api.open-coscientist.com, the
  Railway production environment, the Cloudflare Worker and DNS, the R2 bucket.
- `railway status`, `railway logs` and read-only local SQLite queries are
  routine. Ask and wait for a yes before `railway up`, `deploy`, `redeploy`,
  `restart`, `run`, `down`, variable writes, deletions, or any Cloudflare deploy
  or DNS, Worker or R2 change, including through Railway or Cloudflare tools.
  A restart drops live SSE streams. Ambiguous coordinator leases pause for
  owner-confirmed replay; provably free work retries within its existing budget.
- Keep out of commits and shared output: `.env`, `.env.local`, `app/.env`,
  `engine/mcp_server/.env`, provider keys and other secrets
  (`COSCIENTIST_MCP_SHARED_SECRET`, `LOGS_ADMIN_TOKEN`, `BYOK_ENCRYPTION_KEY`,
  `LITESTREAM_R2_*`), `coscientist.db` and its `-wal`/`-shm` files, caches, run
  outputs. Use only the local ports above (browser suites: `:8108`, `:5273`)
  and the two production domains.

### Code, dependencies and documentation

- Use `Any` only for truly dynamic JSON. Contributions are Apache 2.0;
  preserve third-party notices.
- A new runtime dependency goes in its `pyproject.toml` (or
  `requirements/skills.in`); regenerate the lock with the command in
  [requirements/README.md](requirements/README.md), add
  `requirements/licenses.json` entries for it and every new distribution the
  lock pulls in (on the `allowed` list), then run `make test-evaluations` and
  `make docker-build`. Never edit lock hashes by hand. Read
  [advisory reachability](docs/OPERATIONS.md#advisory-reachability) before
  mounting LiteLLM proxy routes or adding FastMCP OAuth or a disk-backed store.
- **Hidden reasons only.** A comment or docstring survives only for a reason
  the code cannot show (why, an invariant, an outside fact), in about two lines.
  Delete Args/Returns/Raises blocks, narration, `Attributes:` lists and dated
  finding or ADR references; incident histories go to `docs/OPERATIONS.md` as
  their lesson. Test names carry the behavior. MCP tool docstrings and schema
  field descriptions are sent to the model, so they stay.

### Git hygiene

Never mention yourself or any other AI tool in commits, pull requests, pushes
or merge messages: no AI co-author trailers or "Generated with" lines, even
when a tool adds them by default. `scripts/ci/git_hygiene.py` checks branch
commit messages and the PR title and body. It rejects tool names as whole words
in any case (so model names such as Claude and words such as "cursor" fail),
attribution trailers, non-merge commit subjects without `<type>(<scope>): `,
and PR titles with any `type:` or `type(scope):` prefix.

- Commits: `<type>(<scope>): <subject>`, type `feat`, `fix`, `docs`,
  `refactor`, `test` or `chore`, e.g. `fix(report-tab): handle missing markdown`.
  Branches: `<type>/<description>`, e.g. `fix/report-tab-empty-state`.
- PR titles are short, standalone and imperative with no type prefix (`Remove
  unused generate endpoints`); bodies say what changed, why, and how it was
  tested.
- **Squash merges.** GitHub's squash copies every branch commit message into
  the merge commit, so a trailer on a branch commit reaches `main` even when
  the PR body is clean. An empty squash body does not help: GitHub then fills
  in the branch commit messages. Always pass an explicit `<type>(<scope>): `
  subject and a non-empty, neutral body, then read the merged message. Keep
  every branch commit clean; the checker never sees the squash message.

### Working preferences

- Preserve uncommitted work: never `git commit --amend`, `git reset`,
  `git stash`, `git checkout <file>` or `git restore <file>` over it; follow-ups
  are new commits. Check `git reflog -5` and `git stash list` before final
  gates. Never automate merges or other hard-to-reverse Git operations end to end.
- Run the app and engine suites one after the other, with no leftover local
  `uvicorn`; never re-run an unchanged suite to reconfirm it. Run named gates
  directly and record their real exit status, not through a pipeline.
- Inspect the primary source before calling a result unverifiable. Before
  declaring a production probe failed, fetch the artifact once and derive the
  assertion from its output.
