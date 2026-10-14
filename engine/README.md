# co-scientist-engine

The `co_scientist` package: the multi-agent hypothesis engine, its durable
runtime and the FastAPI server, plus the reference MCP literature server in
[`mcp_server/`](mcp_server/README.md). It follows the agent design of Google's
AI co-scientist.

Given a research goal, a supervisor plans the run, and specialized agents
generate, review, rank by Elo tournament, evolve and deduplicate hypotheses
until the orchestrator decides to stop and the meta-review writes the research
overview. Each hypothesis carries `text`, a lay `explanation`,
`literature_grounding` with `[C*]` citation keys, a suggested `experiment`, a
`citation_map` resolving those keys to sources, and any `enrichments`.

- [Engine architecture](docs/ARCHITECTURE.md) — agents, durable workflow,
  orchestration and generation modes
- [Architecture](../docs/ARCHITECTURE.md) — layers, package map and request
  path
- [Engine guide](AGENTS.md) — implementation map and conventions

## Install and develop

`make setup` at the repository root installs this package, which carries the
server's runtime dependencies, editable into `.venv`; `make start` runs it with the UI and MCP
server. To work on the package alone, from `engine/`:

```bash
pip install -e '.[dev]'
pytest
ruff format . && ruff check .
mypy .
```

The package requires Python 3.12+; the API image and the MCP server use 3.12.

## Models

Model strings follow the LiteLLM convention (`provider/model`). Operator runs
use free OpenRouter, subscriber credit, then Azure when each slot is configured
and admitted. BYOK selects its own model and credential. Model capabilities,
routing and prices live in `src/co_scientist/platform/llm/profile/`.
Select `COSCIENTIST_TEST_DOUBLE=deterministic` for private local tests. A
production request with no available provider returns "No model is available
right now".

## Literature retrieval

Literature review and tool-calling generation need an MCP server at
`MCP_SERVER_URL` (default `http://localhost:8888/mcp`). The reference server's
setup, tools and configuration are in [its README](mcp_server/README.md). If
the server is unreachable, the run skips literature review and reflection and
generates from the model alone.

The tool registry is declared in
`src/co_scientist/platform/retrieval/config/tools.yaml`, with
`${VAR:-default}` environment substitution; `schema.py` beside it defines the
format.
