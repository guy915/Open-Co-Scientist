# Architecture

Co-Scientist is one Python package, `co_scientist` (source in
`engine/src/co_scientist/`), served by a FastAPI process, plus a React
workbench (`app/frontend/`) and a reference MCP literature server
(`engine/mcp_server/`). `app/` holds no Python package: it carries the
frontend, the API test suite, operator scripts (`app/dev/`) and the dev
Docker setup. The scientific agents and the durable workflow are described in
[the engine architecture](../engine/docs/ARCHITECTURE.md). Terms are defined
in [GLOSSARY.md](GLOSSARY.md); the decisions behind this shape are the
[ADRs](adr/).

## Layers

A layer imports only the layers below it. `make arch` checks this with
import-linter (`.importlinter`, [ADR-002](adr/002-layering-enforcement.md));
the ignore list is empty and `evaluations/tests/test_import_contracts.py`
keeps it that way.

| Layer | Package | Holds |
|---|---|---|
| 1 | `co_scientist.main` | Composition root: builds the FastAPI app, lifespan, worker startup |
| 2 | `co_scientist.api` | Routers, SSE, wire contracts, request auth |
| 3 | `co_scientist.orchestration` | Durable task runtime, run lifecycle, node registry, drain and finalize |
| 4 | `co_scientist.science` | One package per agent, plus shared prompts, schemas and scheduling |
| 5 | `co_scientist.domains` | `chat` > `report` > `safety` > `research_state` > `documents` \| `access` \| `feedback` |
| 6 | `co_scientist.platform` | `retrieval` > `llm` \| `sandbox` \| `db` > `telemetry` |
| 7 | `co_scientist.core` | Configuration, constants, errors, run modes, context and async helpers |

The other contracts:

- The science agents (`generation`, `reflection`, `ranking`, `evolution`,
  `proximity`, `meta_review`, `supervisor`, `safety_screen`) do not import each
  other. They share `science/prompts`, `science/schemas`, `science/scheduling`
  and the `science/*.py` leaves.
- `fastapi` and `starlette` only in `api` and `main`; `litellm` only in
  `platform/llm`; `httpx` only in `platform/llm` and `platform/retrieval`;
  `sqlite3` only in `platform/db`, `domains` and `orchestration`.

A lower layer that needs something from a higher one takes it by registration
at import time, never by importing upward:

- `platform/retrieval/__init__.py` calls `llm.tool_effects.set_registry_lookup`
  so the gateway can classify tool calls.
- `science/prompts/__init__.py` calls
  `retrieval.evidence.relevance.register_judgment_prompt` with the literature
  relevance prompt and schema.
- `platform.db` exports `Connection` and `Error` so layers without `sqlite3`
  can annotate a connection or catch its base error.
- Functions that read a few `WorkflowState` keys take a structural `TypedDict`
  slice (`SearchState`, `ProgressState`) instead of the full state.

## Package map

```
co_scientist/
  main            composition root: FastAPI app, lifespan, worker startup
  serving         production ASGI factory: trusted-proxy visitor address
  api/            runs/ (lifecycle, read, chat, SSE), interviews/, contracts/,
                  tracing (HTTP spans),
                  documents, uploads, free_usage, byok_models, feedback_api,
                  logs_api, diagnostics, auth, operator_access, request_limits,
                  launch_admission, launch_control_api, trusted_proxy
  orchestration/  engine_tasks/ (durable node, fan-out and match executor,
                  report_finalize), task_worker/ (leased cohorts),
                  repository/ (tasks, tasks_lifecycle, events, receipts, runs_views),
                  engine_adapter/, generator/, registry, workflow_topology,
                  checkpoint, drain, safety_gate, task_runtime
  science/        generation/ reflection/ ranking/ evolution/ proximity/
                  meta_review/ supervisor/ safety_screen/  (agents)
                  prompts/ schemas/ scheduling/  research_model, citations,
                  evidence_context, review_summary, node_degradation
  domains/
    chat/         interviews/, qa/ (run Q&A), repository/, seed/ (example chats)
    report/       build, content, gates, markdown/, repository
    safety/       rules, semantic, gate, hypothesis/, monitor
    research_state/ state (WorkflowState), models/, repository/, drain/,
                  claims/ (grounding and verification), elo, proximity_edges
    documents/    ingest, pdf, staged, repository, extraction admission and
                  worker limits
    access/       credentials, free_usage, byok_models, retention
    feedback/     repository
  platform/
    retrieval/    evidence/ (search, fusion, relevance), research/ (loop),
                  mcp_client/, citations/, tools/, connectors, article
    llm/          request/ (wire policy, thinking), profile/ (ModelProfile),
                  structured/, tools/, admission/, attempts/, offline/,
                  provider_usage, llm_request, scoped_loop, tool_effects
    sandbox/      confinement (landlock, seccomp, cgroups, seatbelt modules), runner,
                  workspace/, skills/, patch/
    db/           schema, models, runs, checkpoints, supervisor_plan,
                  admission, call_admission, storage_admission,
                  logs, log_capture, retrieval_calls
    telemetry/    logging_setup, error_tracking, tracing, progress,
                  diagnostic_events
  core/           config, constants/, exceptions, run_modes/, metrics,
                  byok_scope, env_vars, json_schema, backoff, async_bridge, sse
```

## Request path

```
React workbench (app/frontend)
  |  HTTP + SSE (fetch with X-Client-ID)
  v
co_scientist.main ──> co_scientist.api.*   routers validate, authorize, enqueue
                 |
                 v
        orchestration.task_worker          leased cohorts drain scientific_tasks
                 |
                 v
        orchestration.engine_tasks         one durable task per node, fan-out
                 |                         item and tournament match
                 v
        science.<agent> operations         prompts, schemas, scoring
                 |
                 v
   domains.* repositories     platform.llm      platform.retrieval ──> MCP server
   (SQLite through platform.db)  (LiteLLM)       (literature, web search)
```

## Pipeline events (canonical timeline)

Every run executes through the durable task queue, and the same event-log
table is written whichever LLM backend (offline or real) is behind it. A run
produces this sequence:

```
1.  lifecycle       (created)                    written by POST /api/runs
2.  lifecycle       (queued)                     written by POST /{id}/start
3.  safety.intake   (allow/redact/block)
4.  scientific_task (task=bootstrap)
5.  scientific_task (task=supervisor,        successor=generate)
    scientific_task (task=generate,          successor=review)
    scientific_task (task=review,            successor=comprehensive_reflection)
    …                                            one row per durable node commit,
    scientific_task (task=research_overview, successor=null)
6.  safety.hypothesis  (per-hypothesis screen counts)
7.  citation.grounding (claim-grounding counts)
8.  citation_audit     ({verified, partial, unsupported, unavailable})
9.  safety.final
10. report             (structured payload + markdown)
11. status             (completed)
```

Events 1-2 come from the HTTP layer. Events 3-5 come from the worker:
`safety.intake` is the `engine.bootstrap` task's first act
(`orchestration/engine_tasks/inputs.py::_screen_bootstrap_intake`, which is
also where the run flips to `running`), then one `scientific_task` per node
commit. Events 6-11 come from the terminal `engine.finalize` task: 6-8 from
`orchestration/engine_tasks/finalize.py::_finalize_stage_events` and 9-11
from `orchestration/engine_tasks/report_finalize.py::finalize_report`. This is
why the citation audit lands after `research_overview`. There is no
`status (running)` event: the transition is a `runs` row update.

Every engine node reports under the single `scientific_task` type, carrying the
node it completed in `payload.task` and the node it scheduled next in
`payload.successor` (`orchestration/engine_tasks/support.py::_emit_node_completion`). The
named stage vocabulary (`supervisor.plan`, `literature_review`, `generate`,
`ranking`, …) survives only as milestone chat messages appended by
`orchestration/engine_adapter/events.py::append_node_milestone`; no
`run_events` row carries those types.

Which nodes appear, and how often, is the orchestrator's decision:
`review → comprehensive_reflection → safety_screen → deep_verification →
ranking → orchestrator` recurs once per cycle, `meta_review → evolve` precedes
a re-review, `proximity` runs only when the pool grew since the previous pass,
and `literature_review`/`reflection` are absent when no MCP server is reachable.

`GET /api/runs/{id}/events?after=<seq>` replays history from the requested
sequence, then tails live, so reopening after a restart never depends on
in-memory event state.

## Persistence model

One SQLite file in WAL mode (`platform/db/schema.py`). Each table has one
owning module; other modules go through it.

| Tables | Owner | Notes |
|---|---|---|
| `runs`, `run_metrics` | `platform/db/runs.py`, `retrieval_calls.py` | mutable status, error and timestamps |
| `scientific_tasks` | `orchestration/repository/tasks*.py` | the durable queue: every node, fan-out item and match is a leased, idempotent row |
| `run_events` | `orchestration/repository/events.py` | append-only event log keyed `(run_id, seq)` |
| `run_creation_receipts` | `orchestration/repository/receipts.py` | idempotent run creation |
| `checkpoints` | `platform/db/checkpoints.py` | append-only, pruned; `WorkflowState` after each committed task, the resume point |
| `supervisor_plan`, `supervisor_allocations` | `platform/db/supervisor_plan.py` | the plan and the ledger of scheduled tasks with the stats behind each decision |
| `hypotheses`, `hypothesis_state` | `domains/research_state/repository/hypotheses.py` | `hypotheses` is append-only with `parent_id` lineage; Elo, wins and status live in `hypothesis_state` |
| `evidence`, `citations`, `reviews`, `matches`, `claim_evidence`, `proximity_edges`, `safety_decisions` | `domains/research_state/repository/records.py` | append-only research records |
| `reports`, `knowledge_facts` | `domains/report/repository.py` | structured JSON and rendered Markdown |
| `messages`, `run_announcements`, `interviews`, `interview_turns` | `domains/chat/repository/` | steering and milestones are append-only; Q&A can rewind |
| `staged_documents` | `domains/documents/repository.py` | owner-scoped uploads |
| `run_credentials`, `free_run_usage` | `domains/access/` | encrypted BYOK keys and the free-generation allowance |
| `feedback`, `feedback_admissions` | `domains/feedback/repository.py` | newest 200 within 10 MiB for 30 days; rolling-minute budgets |
| `*_admissions`, `app_llm_usage` | `platform/db/admission.py`, `call_admission.py`, `storage_admission.py` | durable admission and spend ceilings |
| `launch_control` | `platform/db/launch_control.py` | one operator pause and visitor notice row |
| `retrieval_calls`, `app_logs` | `platform/db/retrieval_calls.py`, `logs.py` | per-run retrieval provenance and captured logs |

`hypothesis_state` is the critical decoupling: it holds the values that must
change as the run progresses without mutating the historical hypothesis row.

## Provider selection

`orchestration.engine_adapter.select_provider()` always returns `"engine"`;
the engine is a hard runtime dependency. What varies per run is the LLM
backend. `platform/llm/process_mode.py::offline_mode()` is true when
`COSCIENTIST_TEST_DOUBLE=deterministic` explicitly selects the private test
adapter. Missing keys never select it; production returns a no-model error. An offline run still executes the real durable engine:
`platform/llm/offline/llm.py::install_offline_router()` serves deterministic,
schema-valid completions for `offline/` models. The resolved backend
(`offline` | `real`) is persisted per run as `llm_backend` and reported at
`/status`. Every model fact is one `ModelProfile` in `platform/llm/profile/`;
`platform/llm/request/thinking.py` applies the routing policy
([ADR-004](adr/004-llm-gateway.md)).

## Curated example chats

The three examples ship as `domains/chat/data/demo_runs.json.gz` and are
inserted at startup by `domains/chat/seed`, which replaces a demo run whose
version stamp is older. Titles begin `Example: `. `/examples/:id` requests
`POST /api/runs/{id}/example-chat` and navigates to the visitor's own copy.

`domains/chat/repository/examples.py` copies the curated records and
transcript in one transaction, remapping identities and lineage, and reuses
one copy per owner and source. No engine tasks, credentials, logs or free
allowance are copied or consumed, and no provider or retrieval call is made.
Shared examples allow reads and this copy endpoint; other mutations return 403.

## Run chat context

Run Q&A reads a consistent SQLite snapshot without draining the engine or
taking its write lock. Active runs use the latest compatible checkpoint for
hypotheses, reviews and matches; published rows supply the rest.
`domains/chat/qa/snapshot.py` selects scientific channels; runtime handles,
routing and credentials never enter chat context.

The initial prompt is bounded to 24,000 characters plus grounding rules.
`search_ideas` returns short idea bodies; `search_run_artifacts` searches or
pages every record (three 2,400-character chunks per lookup). At most four
tool calls share one lookup round, then one answer round under the Q&A spend
scope. The numbered evidence manifest is the sole citation namespace.

## Frontend

React 19, Vite 8, Tailwind v4 and Bun. `app/frontend/src/` is split into
`app/` (shell, routes in `workbench_app.tsx`), `features/` (`chat`, `report`,
`runs`, `access`, `diagnostics`, `legal`) and `shared/` (`api`, `hooks`, `lib`, `ui`).
Wire types are generated from the backend contracts
(`app/tests/test_architecture.py` checks they match).

The workbench caches no run or hypothesis data. A run view fetches status and
its collections in parallel, then streams `/api/runs/{id}/events?after=0`
through `shared/api/runs.ts::readSseFrames` (a `fetch`, because the stream
carries `X-Client-ID` and `EventSource` cannot set headers). A hard refresh,
a backend restart and a new browser session all render the same content.
Browser storage holds identity, preferences, provider keys and UI bookkeeping:
the client id, theme, provider and model choices, and the Logs session baseline.
BYOK keys persist locally across reloads and tabs until removed in Settings or
by clearing site data. The legacy single-key vault migrates to the
per-provider vault. Blocked storage falls back to page memory.

## Science operations

Agents expose operations; `orchestration/engine_tasks` owns durable
scheduling, leases, retry keys, partial-failure isolation and checkpoint
commits. Network work runs before the store transaction.

- **Generation** (`science/generation/operations.py`): `prepare_generation`,
  `finalize_generation` and the plan, count and result types own validation,
  allocation, citation context, enrichment and lineage.
- **Ranking**: preparation, remaining-round budgets, deterministic pairing,
  one-match judging and Elo application. The durable adapter judges a wave
  against one median snapshot and commits surviving outcomes in wave order.
- **Reflection**: initial-review gates, verification selection and one-item
  verification, observation and mature-review operations. Ordinary item
  failures preserve siblings; platform-cap and call-budget errors reach the
  worker.
- **Evolution** (`science/evolution/operations.py`): `EvolutionContext` and
  round-context assembly, including guidance, citation sources, parent
  validation and duplicate guards.

Generation and Reflection gather literature through the same
`platform/retrieval/evidence/search.py::collect_papers`; Reflection keeps a
small probe budget and skips the semantic relevance pass.

## Why this shape

- Every run drives the engine through the durable task queue, so a process
  restart resumes from the last committed task
  ([ADR-003](adr/003-durable-runtime.md)).
- Only the completion backend differs between offline and real runs, which
  gives CI, tests and demos the full workflow without provider keys.
- One package with enforced layers replaced two distributions joined by
  adapters in both directions ([ADR-001](adr/001-module-map.md)).
- Production is single-writer SQLite at one api replica; see
  [DEPLOYMENT.md](DEPLOYMENT.md) and [OPERATIONS.md](OPERATIONS.md).
