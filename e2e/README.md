# Browser tests

Playwright suite for the workbench. It starts its own API and UI against a
throwaway store, so it never touches `coscientist.db` or a running
`make start` stack.

```bash
make setup                # once: backend venv and frontend dependencies
make e2e                  # development server suite (tests/)
COSCI_E2E_BROWSER_SHARD=1/8 make e2e # one weighted development shard, as in CI
make e2e-production       # built assets under vite preview (production/)
make e2e-production E2E_ARGS=--shard=1/2 # one production shard, as in CI
make e2e E2E_ARGS="tests/03_run_lifecycle.spec.ts --headed"
```

`make e2e` installs this folder's locked dependencies, typechecks the
harness and installs Playwright's Chromium before running.

## What the harness guarantees

- **Isolated ports.** The API listens on 8108 and the UI on 5273, away from
  the development ports. Override with `COSCI_E2E_API_PORT` and
  `COSCI_E2E_UI_PORT`; run suites one at a time on the same ports.
- **Fresh state.** Each invocation creates one temporary directory
  for the SQLite store and build output. `run.mjs` waits for Playwright
  and its managed servers to stop before removing that directory; an inherited
  `COSCI_E2E_STATE_DIR` is never reused or removed.
- **Offline.** The API runs with `COSCIENTIST_TEST_DOUBLE=deterministic`,
  `EVIDENCE_RESOLVER=offline`, dotenv loading disabled and an unreachable MCP
  URL, so leaked credentials cannot reach a provider.
- **Serial.** One worker, because every spec shares the same servers and
  store.
- **Production build kept apart.** `make e2e-production` builds the frontend
  into the state directory with the test API URL baked in, so the normal
  `app/frontend/dist/` is untouched. It checks deep links, report reloads and
  ownership isolation.

`COSCI_E2E_CHROMIUM_EXECUTABLE` points the suite at an installed browser when
downloads are unavailable; its revision can differ from CI's.

## Layout

- `tests/` — development-server specs, one file per feature area.
- `production/` — built-asset specs.
- `run.mjs` — isolated state, test invocation and cleanup after server shutdown.
- `support/paths.ts` — ports, paths and the raw-CLI state fallback.
- `support/shards.ts` — deterministic whole-file duration-weighted assignment.
- `support/fixtures.ts` — the API fixture, viewports and helpers such as
  `createCompletedRun`.

Development CI uses eight weighted shards from `support/shard_weights.json`.
Weights are median case-duration sums from five successful comprehensive
runs. An unlisted new spec receives 20 seconds and remains included. The planner discovers
all development specs, so no manifest can omit a new test. Every shard keeps
one worker and whole stateful files. Production uses two whole-file shards;
their union contains every production case exactly once.

Use `make e2e` or `BUN=bun node e2e/run.mjs` for cleanup after server shutdown.
Raw `bun x playwright test` creates an isolated fallback directory but leaves it
for manual cleanup. Use the wrapper when supplying a separate Playwright config.

Native WebKit, Firefox and iPhone guards use three whole-file bins per project
in CI. To run a matching bin from the repository root:

```bash
COSCI_E2E_PRODUCTION=1 .venv/bin/python scripts/ci/native_browser_shards.py --project webkit --shard 1 --shards 3
```

The local CLI defaults to two bins when `--shards` is omitted. It discovers
selected cases dynamically, so new tagged files remain included; `--list`
checks the assignment without starting servers.
