# Co-Scientist MCP Server

A Python 3.12 MCP server for literature retrieval, biomedical databases,
knowledge graphs, and web research. FastMCP serves `http://localhost:8888/mcp`;
`GET /` reports the tools and integrations this process actually exposes.

## Run locally

From the repository root:

```bash
cp engine/mcp_server/.env.example engine/mcp_server/.env
# Edit the copied file for provider credentials and cache settings.
make dev-mcp
curl http://localhost:8888/
```

`make start` runs MCP with the API and workbench. For a standalone install:

```bash
python3.12 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -e engine/mcp_server
cd engine
COSCIENTIST_MCP_ALLOW_UNAUTHENTICATED_LOCAL=1 ../.venv-mcp/bin/python -m uvicorn mcp_server.server:app --host 127.0.0.1 --port 8888
```

Add `--reload` for development. To use the engine's Docker Compose service,
copy the environment file as above, then run from `engine/`:

```bash
docker compose up -d --build
docker compose logs -f
# Stop the service when finished:
docker compose down
```

## Tools and configuration

The registry in [server.py](server.py) supplies both FastMCP registration and
the status manifest. It includes:

- Literature: PubMed metadata and PMC fulltext, OpenAlex, Europe PMC and
  preprints, arXiv, and OpenCitations citation edges.
- Biomedical databases: ChEMBL, UniProt, STRING interactions, Reactome
  pathways, Open Targets, Ensembl genes, gnomAD constraints, GWAS Catalog,
  and ClinicalTrials.gov.
- Web: `read_url`, plus `search_web` and `check_web_search_available` when
  a Brave or Tavily key is configured.

[.env.example](.env.example) documents the full configuration. The server
loads a `.env` beside that template; the app's environment file is separate.
`ENTREZ_EMAIL` identifies PubMed traffic and `ENTREZ_API_KEY` increases its
rate allowance; anonymous access remains available. TLS verification stays
on, and `DISABLE_SSL_VERIFY=true` is rejected.

`WEB_SEARCH_PROVIDER` selects a preference. With both keys set, credential
or quota refusal falls through to the next provider; a successful empty
answer does not. Refusal covers HTTP 401/402/403, Tavily 432/433 and Brave's
`QUOTA_LIMITED`, `USAGE_LIMIT_EXCEEDED` or `CREDIT_EXHAUSTED` codes (sent with
429). Any other 429 moves only that query to the next provider, without retry.
A refused provider is asked again once every provider has refused. A Brave
quota refusal is also lifted for the first search after the longest window in
its `X-RateLimit-Reset` header (seconds from now, e.g. `1, 1419704`), or after
24 hours when that header is missing or unreadable; a key refusal stays until
restart.
`check_web_search_available` reports observed refusals.

`COSCIENTIST_MCP_SHARED_SECRET` must match on server and engine; MCP calls
require the `X-MCP-Shared-Secret` header. Without it, every request except
`GET /` is refused. `make start` and `make dev-mcp` explicitly enable
`COSCIENTIST_MCP_ALLOW_UNAUTHENTICATED_LOCAL=1`, which permits actual loopback
peers only when no secret is configured. Never set this flag in production.
Deployment details live in [DEPLOYMENT.md](../../docs/DEPLOYMENT.md).

The engine defaults to `http://localhost:8888/mcp`; override it with
`MCP_SERVER_URL`. New tools also need matching declarations in the engine's
`src/co_scientist/platform/retrieval/config/tools.yaml`.

## Retrieval implementation

`PubmedSource` coordinates metadata, PMC downloads, and shared-pool storage.
Papers accumulate under `<cache>/pubmed/<slug>/shared/`; each run links to
those files under `runs/<run_id>/`. The fulltext tool extracts cached JATS
into markdown, keeping abstracts and section headings. Set
`include_fulltext=false` to retain metadata selection and provenance while
skipping downloads and extraction.

`tools/lit_review/` implements the literature providers, and `tools/web_providers.py` and `tools/web_fetch.py` handle search and page extraction.
`read_url` screens resolved addresses and every redirect before fetching.
The registration wrapper logs tool outcomes while preserving the signatures
FastMCP uses to advertise parameters.

## Validation

```bash
make test-mcp
```

This runs the MCP package's offline pytest suite and strict mypy with its
Python 3.12 environment. The engine's own tests do not include this package.
