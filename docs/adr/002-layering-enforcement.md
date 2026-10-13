# ADR-002: Layering and how it is enforced

**Status:** Accepted.

## Context

Layering rules that live in prose decay, and most of this code is written by
agents. ADR-001 sets the layers; this ADR sets the tool and the contracts.

## Decision

**Tool: `import-linter`** (a dev dependency of the engine distribution). It
builds the import graph with `grimp`, counts function-local and
`TYPE_CHECKING` imports like any other, and reports every broken contract with
its import chain.

**`make arch`** runs `lint-imports` against the root `.importlinter`
configuration with `--no-cache`. `make lint` runs it, and CI runs it in the
typecheck job. It needs no network and no keys.

**Six contracts**, all over plain `co_scientist.*` module names:

1. **Layers:** `main` > `api` > `orchestration` > `science` > `domains.chat` >
   `domains.report` > `domains.safety` > `domains.research_state` >
   (`domains.documents` | `domains.access` | `domains.feedback`) >
   `platform.retrieval` > (`platform.llm` | `platform.sandbox` |
   `platform.db`) > `platform.telemetry` > `core`. Layers joined by `|` are
   independent siblings. This contract carries the domain and platform
   sub-orders of ADR-001.
2. **Independence** of the eight science agent packages.
3. **`fastapi`/`starlette`** forbidden outside `api` and `main`.
4. **`litellm`** forbidden outside `platform.llm`.
5. **`httpx`** forbidden outside `platform.llm` and `platform.retrieval`.
6. **`sqlite3`** forbidden in `main`, `api`, `science`, `platform.retrieval`,
   `platform.llm`, `platform.sandbox`, `platform.telemetry` and `core`.

The third-party contracts set `allow_indirect_imports`, so they restrict who
imports the package directly, not who reaches it through a permitted module.
No contract carries an `ignore_imports` entry.
`evaluations/tests/test_import_contracts.py` holds `IGNORED_IMPORTS_CEILING = 0`
and asserts the count equals it, so adding an exception fails the test, and
any future shrink must lower the constant in the same change. It also checks
that each entry names a single import.

Finer rules stay as AST tests beside the code they protect:
`engine/tests/test_agents.py` orders the `platform/llm` subpackages and
rejects module-level cycles inside it, and `app/tests/test_architecture.py`
guards shared-service and composition-root imports.

## Consequences

- A prose rule that a contract enforces points to `make arch` instead of
  restating the rule.
- Lazy imports do not hide cycles: function-local imports are part of the
  graph.
- Adding a layer-crossing import shows up as a red `make arch` with the import
  chain, not as a review comment.
- The contracts do not check that other packages import only from a package's
  `__init__`.
