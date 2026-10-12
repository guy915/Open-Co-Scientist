from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from co_scientist.api import diagnostics
from co_scientist.api.operator_access import is_operator
from co_scientist.api.version import API_VERSION
from co_scientist.core.config import settings
from co_scientist.orchestration import engine_adapter
from co_scientist.orchestration.notifications import email_notifications_configured

router = APIRouter()


class HealthCheckResult(BaseModel):
    """Outcome of one health check."""

    ok: bool = Field(..., description="whether the check passed")
    detail: str | None = Field(None, description="failure detail when the check did not pass")


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(..., description="derived health: healthy | degraded | unhealthy")
    version: str
    model_name: str | None = Field(
        None, description="configured worker model id (operator callers only)"
    )
    provider: str = Field(..., description="active workflow provider (always 'engine')")
    checks: dict[str, HealthCheckResult] = Field(
        ...,
        description=(
            "individual check outcomes: store, engine, queue, disk. Only "
            "store can drive an unhealthy status (503); the rest degrade"
        ),
    )


class ProbeStatus(BaseModel):
    """Detailed outcome of one availability probe."""

    state: str = Field(
        ...,
        description=(
            "probe outcome: 'up' | 'down' (definitive answers) | 'error' "
            "(the probe itself failed; availability unknown)"
        ),
    )
    error: str | None = Field(None, description="probe failure detail when state is 'error'")


class Connector(BaseModel):
    """One data-source connector shown in the composer's connectors menu."""

    id: str = Field(..., description="stable connector id")
    display: str = Field(..., description="human-readable connector name")


class SystemStatusResponse(BaseModel):
    """System availability status response."""

    mcp_available: bool = Field(..., description="whether mcp server is available")
    pubmed_available: bool = Field(..., description="whether pubmed api is available")
    literature_review_available: bool = Field(
        ...,
        description=("whether literature review is available (requires both mcp and pubmed)"),
    )
    web_search_available: bool = Field(
        False,
        description=(
            "whether a web search issued now would reach a provider -- not "
            "merely whether the tool is advertised, which stays true after "
            "the provider starts refusing the key"
        ),
    )
    email_notifications_available: bool = Field(
        False,
        description="whether public completion-email delivery is available",
    )
    # Operator-only diagnostics expose internal routing, configuration and
    # credential presence; public callers receive null.
    probes: dict[str, ProbeStatus] | None = Field(
        None,
        description=(
            "per-probe detail (mcp, pubmed, web_search), distinguishing a "
            "served 'down' from a probe error; operator callers only"
        ),
    )
    mcp_server_url: str | None = Field(
        None, description="configured mcp server url; operator callers only"
    )
    provider: str = Field("engine", description="active workflow provider (always 'engine')")
    llm_backend: str = Field(
        "real",
        description="active LLM backend: 'offline' (deterministic) | 'real'",
    )
    has_provider_key: bool | None = Field(
        None, description="any LLM provider key is set; operator callers only"
    )
    byok_enabled: bool | None = Field(
        None,
        description=(
            "whether this deployment accepts bring-your-own-key runs "
            "(the credential encryption secret is configured); operator "
            "callers only"
        ),
    )
    engine_importable: bool | None = Field(
        None,
        description="co_scientist package is importable; operator callers only",
    )
    model_name: str = Field("", description="configured worker model id")
    supervisor_model_name: str | None = Field(
        None,
        description=("effective supervisor/meta-review model id; operator callers only"),
    )
    enabled_tools: list[str] | None = Field(
        None,
        description=(
            "enabled tool ids from the default tools config, else null "
            "(engine unavailable/redacted); operator callers only"
        ),
    )
    connectors: list[Connector] = Field(
        default_factory=list,
        description=(
            "user-facing data-source connectors derived from availability and "
            "the default tools config, for the composer's connectors menu"
        ),
    )


@router.get("/", tags=["root"])
async def root() -> dict[str, str]:
    return {"message": "Co-Scientist API", "version": API_VERSION}


def _redact_health_check(check: HealthCheckResult, operator: bool) -> HealthCheckResult:
    """Failure details can reveal paths, exceptions or run IDs; public
    health reads need only the verdict.
    """
    if operator:
        return check
    return HealthCheckResult(ok=check.ok, detail=None)


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health(request: Request, response: Response) -> HealthResponse:
    """Health check: liveness plus a durable-queue and disk read.

    Every check is local and fast (a SQLite round-trip, an import lookup,
    a read-only queue aggregate, a disk stat) because ``make start`` polls
    this endpoint as its readiness gate and a deploy platform polls it
    continuously thereafter. Only store unreachability drives a 503: a
    wedged run, a terminally failed task, or low disk are real problems
    worth surfacing, but a failed healthcheck kills the container
    mid-run, so they report ``degraded`` at 200 rather than take the
    process down. The deploy probe only ever reads the status code, so
    gating check detail and the model name behind operator access below
    does not touch what makes this endpoint useful as a healthcheck.
    """
    operator = is_operator(request)
    store_check = diagnostics.check_store()
    engine_check = diagnostics.check_engine()
    queue_check, disk_check = diagnostics.queue_and_disk_health_cached()
    status = diagnostics.derive_overall_health(store_check, engine_check, queue_check, disk_check)
    if status == diagnostics.UNHEALTHY:
        response.status_code = 503
    checks = {
        "store": HealthCheckResult(ok=store_check.ok, detail=store_check.detail),
        "engine": HealthCheckResult(ok=engine_check.ok, detail=engine_check.detail),
        "queue": HealthCheckResult(ok=queue_check.ok, detail=queue_check.detail),
        "disk": HealthCheckResult(ok=disk_check.ok, detail=disk_check.detail),
    }
    return HealthResponse(
        status=status,
        version=API_VERSION,
        model_name=settings.model_name if operator else None,
        provider=engine_adapter.select_provider(),
        checks={name: _redact_health_check(check, operator) for name, check in checks.items()},
    )


def _local_capabilities() -> dict[str, Any]:
    return {
        "email_notifications_available": email_notifications_configured(),
    }


def _build_status_payload(
    mcp: diagnostics.ProbeResult,
    pubmed: diagnostics.ProbeResult,
    web_search: diagnostics.ProbeResult,
    literature_available: bool,
    adapter_status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "mcp_available": mcp.available,
        "pubmed_available": pubmed.available,
        "literature_review_available": literature_available,
        # Tool registration alone does not prove the configured provider will
        # accept a search now.
        "web_search_available": web_search.available,
        **_local_capabilities(),
        "probes": {
            "mcp": {"state": mcp.state, "error": mcp.error},
            "pubmed": {"state": pubmed.state, "error": pubmed.error},
            "web_search": {
                "state": web_search.state,
                "error": web_search.error,
            },
        },
        "connectors": engine_adapter.connectors_report(
            literature_available=literature_available,
            enabled_tools=adapter_status.get("enabled_tools"),
            web_search_available=web_search.available,
        ),
        **adapter_status,
    }


# Public UI capability fields remain visible; deployment internals and probe
# error text require operator access.
_PUBLIC_STATUS_FIELDS = frozenset(
    {
        "mcp_available",
        "pubmed_available",
        "literature_review_available",
        "web_search_available",
        "email_notifications_available",
        "connectors",
        "provider",
        "llm_backend",
        "model_name",
    }
)


def _redact_status_payload(payload: dict[str, Any], operator: bool) -> dict[str, Any]:
    """Compute public connector summaries before removing the raw operator-
    only configuration they depend on.
    """
    if operator:
        return payload
    return {
        key: (value if key in _PUBLIC_STATUS_FIELDS else None) for key, value in payload.items()
    }


@router.get("/status", response_model=SystemStatusResponse, tags=["system"])
async def get_system_status(request: Request) -> dict[str, Any]:
    """Checks system availability for literature review features.

    Returns availability status for mcp server and pubmed api, plus
    provider/llm-backend info from the engine adapter so the UI can render
    an "Offline mode" chip. Probes run under a bounded timeout and are
    cached for a short TTL (see app/diagnostics.py); the ``probes`` field
    distinguishes a server that answered "down" from a probe that errored.
    Fields with no use in the product's own UI are visible only to an
    operator caller (see ``is_operator``); every other caller sees them
    as null.
    """
    mcp, pubmed, web_search = await diagnostics.probe_literature_stack_cached()

    adapter_status = engine_adapter.system_status()

    # Literature tools need both the MCP process and its PubMed-backed source to
    # answer.
    literature_available = mcp.available and pubmed.available

    payload = _build_status_payload(mcp, pubmed, web_search, literature_available, adapter_status)
    return _redact_status_payload(payload, is_operator(request))
