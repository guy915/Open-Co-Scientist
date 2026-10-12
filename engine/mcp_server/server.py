import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastmcp import FastMCP

from mcp_server.log_format import configure_json_logging, route_library_loggers

configure_json_logging()

# Load the server's co-located .env before importing tools that read it.
# Deployments can also supply these variables directly.
logger = logging.getLogger(__name__)
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
    logger.info("Loaded environment from %s", env_path)
else:
    logger.info("No .env file at %s; using the process environment", env_path)

configured_log_level = (
    os.environ.get("COSCIENTIST_MCP_LOG_LEVEL") or os.environ.get("LOG_LEVEL", "INFO")
).upper()
log_level = getattr(logging, configured_log_level, logging.INFO)

# Only this package's logger honors the configured LOG_LEVEL; third-party
# libraries stay at the INFO default set above.
logging.getLogger("mcp_server").setLevel(log_level)

from mcp_server.log_privacy import install_transport_log_privacy

install_transport_log_privacy()

from mcp_server.auth_middleware import (
    MCP_LOCAL_AUTH_ENV,
    SharedSecretAuthMiddleware,
    resolve_shared_secret,
)
from mcp_server.cache_privacy import migrate_literature_cache
from mcp_server.tool_logging import with_call_logging
from mcp_server.tools.biomedical_databases import (
    search_chembl,
    search_clinical_trials,
    search_ensembl_gene,
    search_gnomad_constraint,
    search_gwas_catalog_associations,
    search_open_targets,
    search_reactome_pathways,
    search_string_interactions,
    search_uniprot,
)
from mcp_server.tools.lit_review.arxiv_search import search_arxiv
from mcp_server.tools.lit_review.europepmc_search import (
    search_biorxiv,
    search_europepmc,
    search_preprints,
)
from mcp_server.tools.lit_review.openalex_search import search_openalex
from mcp_server.tools.lit_review.opencitations import (
    get_opencitations_citation_edges,
)
from mcp_server.tools.lit_review.search_pubmed import (
    check_pubmed_available,
    pubmed_search_with_fulltext,
    search_pubmed,
)
from mcp_server.tools.web_fetch import read_url
from mcp_server.tools.web_providers import (
    check_web_search_available,
    resolve_provider,
    search_web,
    web_search_credential_error,
)

entrez_email_present = bool(os.environ.get("ENTREZ_EMAIL"))

# Do not offer key-gated search when no provider exists; read_url remains
# keyless.
_web_provider = resolve_provider()
web_search_provider = _web_provider[0] if _web_provider else None

logger.info("MCP server starting")
logger.debug(
    "API keys present: ENTREZ_EMAIL=%s, web_search_provider=%s",
    entrez_email_present,
    web_search_provider or "none",
)

mcp = FastMCP("co-scientist-lit-review")

# One tuple drives registration order and the root manifest to prevent drift.
_MCP_TOOLS = (
    (check_pubmed_available, "check_pubmed_available"),
    (search_pubmed, "search_pubmed"),
    (pubmed_search_with_fulltext, "pubmed_search_with_fulltext"),
    (search_openalex, "search_openalex"),
    (get_opencitations_citation_edges, "get_opencitations_citation_edges"),
    # Offer the key-health probe only when its search provider is configured.
    *(
        (
            (search_web, "search_web"),
            (check_web_search_available, "check_web_search_available"),
        )
        if web_search_provider
        else ()
    ),
    (read_url, "read_url"),
    (search_chembl, "search_chembl"),
    (search_uniprot, "search_uniprot"),
    (search_string_interactions, "search_string_interactions"),
    (search_reactome_pathways, "search_reactome_pathways"),
    (search_open_targets, "search_open_targets"),
    (search_europepmc, "search_europepmc"),
    (search_preprints, "search_preprints"),
    (search_arxiv, "search_arxiv"),
    (search_biorxiv, "search_biorxiv"),
    (search_ensembl_gene, "search_ensembl_gene"),
    (search_gnomad_constraint, "search_gnomad_constraint"),
    (search_gwas_catalog_associations, "search_gwas_catalog_associations"),
    (search_clinical_trials, "search_clinical_trials"),
)

for _tool_fn, _tool_name in _MCP_TOOLS:
    mcp.tool(with_call_logging(_tool_fn, _tool_name), name=_tool_name)

# Startup logs distinguish absent capabilities from later empty answers.
logger.info(
    "Registered %d MCP tools: %s",
    len(_MCP_TOOLS),
    ", ".join(name for _, name in _MCP_TOOLS),
)
logger.info(
    "PubMed: %s",
    "configured"
    if entrez_email_present
    else "anonymous (ENTREZ_EMAIL unset; reachability not yet checked)",
)

# Reuse FastMCP lifespan so mounting beneath FastAPI preserves initialization
# and cleanup.
# Stateless HTTP avoids restart/replica session affinity.
mcp_http_app = mcp.http_app(stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await asyncio.to_thread(migrate_literature_cache)
    async with mcp_http_app.lifespan(app):
        yield


app = FastAPI(lifespan=lifespan)

# Only server callers use MCP; browsers need no trusted origin or credentialed
# CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Shared secrets add an independent control beyond network placement.
_mcp_shared_secret = resolve_shared_secret()
app.add_middleware(SharedSecretAuthMiddleware, secret=_mcp_shared_secret)
logger.info(
    "MCP shared-secret auth: %s",
    "enabled"
    if _mcp_shared_secret
    else "loopback development exception"
    if os.environ.get(MCP_LOCAL_AUTH_ENV) == "1"
    else "requests refused (shared secret unset)",
)


@app.get("/")
async def root() -> JSONResponse:
    """Returns API status and available tool list.

    Returns:
        A JSON response describing the running service, its version, the
        registered MCP tools, and configured integrations.
    """
    return JSONResponse(
        {
            "status": "running",
            "service": "coscientist-lit-review",
            "version": "0.1.0",
            "mcp_tools": [name for _, name in _MCP_TOOLS],
            "api_keys_configured": {
                "ENTREZ_EMAIL": entrez_email_present,
                "WEB_SEARCH": web_search_provider is not None,
            },
            "integrations": {
                "web_search_provider": web_search_provider,
                # Credential refusals are observed on real searches, not
                # inferred at startup.
                "web_search_credential_error": web_search_credential_error(),
            },
        }
    )


# Mount after root so health JSON wins before the catch-all MCP application.
app.mount("/", mcp_http_app)
route_library_loggers()

if __name__ == "__main__":
    port = int(os.environ.get("COSCIENTIST_MCP_PORT", 8888))
    uvicorn.run(app, host="0.0.0.0", port=port)
