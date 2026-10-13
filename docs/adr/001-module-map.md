# ADR-001: Module map and allowed dependencies

**Status:** Accepted.

## Context

The product is one Python package organized by domain, in layers. Two
distributions joined by adapters in both directions (an engine package and a
viewer package) meant LLM policy sat on both sides, the store lived apart from
the code that used it, and nothing stated which module may import which.

## Decision

**One package, `co_scientist`, with its source at `engine/src/co_scientist/`.**
The composition root is `co_scientist.main` (`Dockerfile.api` runs
`scripts/api-entrypoint.sh`, which starts `co_scientist.serving:create_app`, a
trusted-proxy wrapper around `co_scientist.main:app`). Keeping the engine's
import name keeps logger names (`co_scientist.*`), the sandbox launcher string
and test patch targets stable.

**Layers**, top to bottom. A layer imports only layers below it.

| Layer | Package | Holds |
|---|---|---|
| 1 | `co_scientist.main` | Composition root: builds the FastAPI app, lifespan, wiring |
| 2 | `co_scientist.api` | Routers, SSE, wire contracts, request auth; the only `fastapi` user |
| 3 | `co_scientist.orchestration` | Workflow topology, node registry, durable task runtime (queue, leases, worker cohorts, recovery), run lifecycle, drain, finalize |
| 4 | `co_scientist.science` | One package per agent; shared `prompts`, `schemas`, `scheduling`, `research_model` and `node_degradation` |
| 5 | `co_scientist.domains` | `research_state`, `safety`, `report`, `chat`, `documents`, `access`, `feedback` |
| 6 | `co_scientist.platform` | `retrieval`, `llm`, `sandbox`, `db`, `telemetry` |
| 7 | `co_scientist.core` | Types, errors, configuration, run modes, context and async helpers; no I/O |

**Within layers:**

- `science` agents (`generation`, `reflection`, `ranking`, `evolution`,
  `proximity`, `meta_review`, `supervisor`, `safety_screen`) are independent
  of each other; they share only the modules outside the agent packages.
- `domains` are layered: `chat` > `report` > `safety` > `research_state` >
  `documents` | `access` | `feedback`. These are the only cross-domain edges.
- `platform` is layered: `retrieval` > `llm` | `sandbox` | `db` >
  `telemetry`. `retrieval` may use `llm` (relevance scoring); every adapter
  may use `telemetry`; `llm`, `sandbox` and `db` do not import each other.

**Third parties:** `fastapi`/`starlette` only in `api` and `main`; `sqlite3`
only in `platform/db`, the domains and `orchestration`; `litellm` only in
`platform/llm`; `httpx` only in `platform/llm` and `platform/retrieval`;
`langgraph`/`langchain_*` only in `platform/retrieval`,
`domains/research_state` (state reducers) and `orchestration` (checkpoints);
this last rule is a convention, not an import-linter contract.

**Names.** `.dockerignore` and `.gitignore` drop directories named `cache`,
`reports`, `build` or `dist` while `COPY` still succeeds, so a module with
one of those names would vanish from the image without a build error. The
report domain is therefore `domains/report`, and there is no `platform/cache`.

## Consequences

- `.importlinter` encodes the layers, the independence rule and the
  third-party rules; ADR-002 describes how they are enforced. A layer-crossing
  import fails `make arch` instead of waiting for review.
- `app/` holds only the frontend, its tests and development tooling.
- The engine's Python floor is 3.12, matching the production runtime.
- A package's `__init__.py` is its preferred interface, but no contract
  forbids importing its submodules.
