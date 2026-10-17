from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

import co_scientist.orchestration.engine_adapter as engine_adapter
from co_scientist.api.auth import Principal, principal_for_request
from co_scientist.api.byok_models import router as byok_models_router
from co_scientist.api.deployment_control_api import DeploymentAdmissionMiddleware
from co_scientist.api.deployment_control_api import router as deployment_control_router
from co_scientist.api.diagnostics_api import router as diagnostics_api_router
from co_scientist.api.documents import router as documents_router
from co_scientist.api.feedback_api import router as feedback_router
from co_scientist.api.free_usage import router as free_usage_router
from co_scientist.api.interviews import router as interviews_router
from co_scientist.api.launch_admission import LaunchAdmissionMiddleware, paused_error_handler
from co_scientist.api.launch_control_api import router as launch_control_router
from co_scientist.api.logs_api import router as logs_router
from co_scientist.api.request_limits import RequestLimitsMiddleware, storage_error_handler
from co_scientist.api.runs import router as runs_router
from co_scientist.api.sentry_alerts import router as sentry_alerts_router
from co_scientist.api.spend_api import router as spend_router
from co_scientist.api.tracing import TracingMiddleware
from co_scientist.api.usage_api import router as usage_router
from co_scientist.api.version import API_VERSION
from co_scientist.core import inflight
from co_scientist.core.async_bridge import off_loop
from co_scientist.core.config import settings
from co_scientist.core.exceptions import StorageAdmissionError
from co_scientist.domains.chat.seed import is_current_demo_run, seed_demo_runs
from co_scientist.orchestration.alert_markers import delivery_loop, enabled
from co_scientist.orchestration.repository import runs_views as views
from co_scientist.orchestration.repository import tasks
from co_scientist.platform import db
from co_scientist.platform.db import checkpoints as store
from co_scientist.platform.db import runs
from co_scientist.platform.db.launch_control import LaunchPausedError
from co_scientist.platform.db.log_capture import configure_log_capture, shutdown_log_capture
from co_scientist.platform.db.models import DEMO_CLIENT_ID, RunRow
from co_scientist.platform.telemetry.error_tracking import init_error_tracking
from co_scientist.platform.telemetry.logging_setup import configure_logging, level_to_number
from co_scientist.platform.telemetry.tracing import configure_tracing, shutdown_tracing

logger = logging.getLogger(__name__)


def _reclaim_disk_space() -> None:
    """Keep the latest resume checkpoint and prune opportunistically; never
    VACUUM from the serving process or fail startup on housekeeping.
    """
    try:
        superseded = store.prune_superseded_checkpoints()
    except db.Error:
        logger.warning("Could not prune superseded checkpoints", exc_info=True)
    else:
        if superseded:
            logger.info(
                "Pruned %s superseded checkpoint(s) no run could resume from",
                superseded,
            )


def _startup_engine_setup() -> None:
    from co_scientist.platform.llm.process_mode import offline_mode

    if offline_mode():
        from co_scientist.platform.llm.offline.llm import install_offline_router

        install_offline_router()
    logger.info("Model: %s", settings.model_name)
    provider = engine_adapter.select_provider()
    logger.info("Workflow provider: %s", provider)


def _reconcile_and_log_interrupted_runs() -> dict[str, list[str]]:
    reconciled = views.reconcile_interrupted_runs()
    if reconciled["failed"]:
        logger.info(
            "Reconciled %s interrupted run(s) to failed: %s",
            len(reconciled["failed"]),
            ", ".join(r[:8] for r in reconciled["failed"]),
        )
    if reconciled["resumable"]:
        logger.info(
            "Found %s resumable interrupted run(s) with a checkpoint: %s",
            len(reconciled["resumable"]),
            ", ".join(r[:8] for r in reconciled["resumable"]),
        )
    return reconciled


async def _resume_checkpointed_runs(resumable: list[str]) -> None:
    if not resumable:
        return
    from co_scientist.api.runs import resume_interrupted_runs

    await resume_interrupted_runs(resumable)


def _launch_embedded_recovery_workers(
    recovery_workers: list[asyncio.Task[None]],
) -> None:
    """Recover off the API loop so synchronous checkpoint writes and
    serialization cannot starve request handling or lease renewal.
    """
    import co_scientist.orchestration.task_worker as task_worker

    for run_id in tasks.list_active_engine_task_run_ids():
        recovery_workers.append(
            asyncio.create_task(
                asyncio.to_thread(
                    task_worker.run_run_worker_pool_sync,
                    run_id,
                    f"embedded-recovery:{os.getpid()}",
                )
            )
        )


async def _shutdown_recovery(
    recovery: asyncio.Task[None],
    recovery_workers: list[asyncio.Task[None]],
) -> None:
    recovery.cancel()
    await asyncio.gather(recovery, return_exceptions=True)
    for worker in recovery_workers:
        worker.cancel()
    if recovery_workers:
        await asyncio.gather(*recovery_workers, return_exceptions=True)


def _expire_earlier_process_leases() -> None:
    """A killed or restarted process never releases its leases, and waiting
    out a full lease idles every active run.
    """
    import co_scientist.orchestration.task_worker as task_worker

    try:
        expired = tasks.expire_earlier_process_leases(task_worker.process_tag())
    except Exception:
        logger.warning("Expiring task leases of an earlier process failed", exc_info=True)
        return
    if expired:
        logger.info("Expired %d task lease(s) held by an earlier process", expired)


def _start_recovery_task(
    reconciled: dict[str, list[str]],
) -> tuple[asyncio.Task[None], list[asyncio.Task[None]]]:
    """Recovery is independent of binding a port; startup must not wait for
    provider work.
    """
    recovery_workers: list[asyncio.Task[None]] = []

    async def _recover_runs() -> None:
        """Recovery runs beside serving; awaiting provider work before
        binding creates a healthcheck/restart spiral.
        """
        await asyncio.to_thread(_expire_earlier_process_leases)
        await _resume_checkpointed_runs(reconciled["resumable"])
        _launch_embedded_recovery_workers(recovery_workers)

    recovery = asyncio.create_task(_recover_runs())
    return recovery, recovery_workers


# Load environment before constructing Settings at import time.
load_dotenv()


configure_logging(level=logging.INFO)
init_error_tracking(settings.sentry_dsn, settings.sentry_environment)


def _install_log_capture() -> None:
    if not settings.log_capture_enabled:
        return
    level = level_to_number(settings.log_capture_level) or logging.INFO
    configure_log_capture(level=level, max_rows=settings.log_capture_max_rows)


# Import-time capture includes setup diagnostics; lifespan reinstalls and drains
# it across reloads.
_install_log_capture()

coscientist_logger = logging.getLogger("co_scientist")
logger.setLevel(logging.INFO)
coscientist_logger.setLevel(logging.INFO)

# The engine MCP client reads its URL from environment rather than a Settings
# parameter.
if settings.mcp_server_url:
    os.environ["MCP_SERVER_URL"] = settings.mcp_server_url
    logger.info("mcp_server_url configured: %s", settings.mcp_server_url)
else:
    logger.info("mcp_server_url not set - literature review will be disabled")


async def _privacy_retention_loop() -> None:
    from co_scientist.domains.access.retention import sweep_all

    while True:
        work = asyncio.create_task(asyncio.to_thread(sweep_all))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            # Let the short SQLite transactions finish before shutdown merges
            # WAL; cancellation must not strand a live maintenance writer.
            await work
            raise
        except Exception:
            logger.warning("Privacy retention maintenance failed", exc_info=False)
        await asyncio.sleep(3600)


def _release_leases_for_next_process() -> None:
    """A deploy must not idle every run for a full lease; a task with an
    unanswered provider call keeps its lease so the unknown-outcome rule
    still decides it after expiry.
    """
    import co_scientist.orchestration.task_worker as task_worker

    in_flight = inflight.begin_shutdown()
    try:
        released = tasks.release_owned_leases(
            task_worker.process_worker_ids(), keep_task_ids=in_flight
        )
    except Exception:
        logger.warning("Releasing task leases at shutdown failed", exc_info=True)
        return
    if released or in_flight:
        logger.info(
            "Released %d task lease(s) for the next process; %d kept with a call in flight",
            released,
            len(in_flight),
        )


@asynccontextmanager
async def lifespan(
    app: FastAPI,
) -> AsyncGenerator[None, None]:
    # A prior lifespan cycle may have drained capture, so startup reinstalls it.
    _install_log_capture()
    configure_tracing()
    inflight.resume_dispatch()
    logger.info("Starting Co-Scientist server...")
    _startup_engine_setup()

    # Prune cheaply before readers arrive; never add serving-process VACUUM or
    # truncating checkpoint work.
    _reclaim_disk_space()

    reconciled = _reconcile_and_log_interrupted_runs()
    recovery, recovery_workers = _start_recovery_task(reconciled)

    await seed_demo_runs()
    privacy_retention = asyncio.create_task(_privacy_retention_loop())
    alert_delivery = asyncio.create_task(delivery_loop()) if enabled() else None

    try:
        yield
    finally:
        _release_leases_for_next_process()
        privacy_retention.cancel()
        if alert_delivery is not None:
            alert_delivery.cancel()
            await asyncio.gather(alert_delivery, return_exceptions=True)
        await asyncio.gather(privacy_retention, return_exceptions=True)
        await _shutdown_recovery(recovery, recovery_workers)
        logger.info("Shutting down Co-Scientist server...")
        # Drain queued logging before the shutdown WAL merge.
        shutdown_log_capture()
        shutdown_tracing()
        db.checkpoint_wal()


app = FastAPI(
    title="Co-Scientist API",
    description="FastAPI server for AI hypothesis generation",
    version=API_VERSION,
    lifespan=lifespan,
    # Built-in docs bypass operator policy; replace them with gated routes
    # rather than exposing the full schema publicly.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


DEFAULT_ALLOWED_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173,https://open-coscientist.com"


def _resolve_cors_config(env_value: str) -> tuple[list[str], bool]:
    """Credentialed CORS requires an explicit origin allowlist; wildcard
    credentials would reflect arbitrary origins.
    """
    if not env_value:
        return ["*"], False
    origins = [o.strip() for o in env_value.split(",") if o.strip()]
    return origins, True


_allowed_origins, _allow_credentials = _resolve_cors_config(
    os.getenv("ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS)
)


@off_loop
def _lookup_run(run_id: str) -> RunRow | None:
    return runs.get_run(run_id)


async def _run_ownership_response(request: Request, principal: Principal) -> Response | None:
    """Empty subjects own nothing, including legacy empty-owner rows; non-
    owned run existence remains hidden.
    """
    parts = request.scope["path"].strip("/").split("/")
    if len(parts) < 3 or parts[:2] != ["api", "runs"]:
        return None
    run_id = parts[2]
    if run_id == "demo":
        return None
    run = await _lookup_run(run_id)
    if run is None:
        return None
    if run.client_id == DEMO_CLIENT_ID:
        if not is_current_demo_run(run.id):
            return JSONResponse({"detail": "run not found"}, status_code=404)
        if request.method not in {"GET", "HEAD"} and not (
            request.method == "POST" and parts[3:] == ["example-chat"]
        ):
            return JSONResponse({"detail": "shared examples are read-only"}, status_code=403)
        return None
    client_id = principal.subject
    from co_scientist.api.operator_access import has_admin_token

    if (
        request.method == "POST"
        and len(parts) == 6
        and parts[3] == "safety"
        and parts[5] == "adjudicate"
        and has_admin_token(request)
    ):
        return None
    if client_id and client_id == run.client_id:
        return None
    return JSONResponse({"detail": "run not found"}, status_code=404)


@app.middleware("http")
async def enforce_run_ownership(request: Request, call_next: Any) -> Response:
    # CORS preflights contain header names only; authorize the real request
    # after the middleware handles them.
    if request.method == "OPTIONS":
        return cast(Response, await call_next(request))
    # Authorize routed ASGI paths, not URLs reconstructed from caller-controlled
    # Host headers.
    try:
        principal = principal_for_request(request)
    except HTTPException as exc:
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )
    ownership_response = await _run_ownership_response(request, principal)
    if ownership_response is not None:
        return ownership_response
    from co_scientist.platform.db.admission import connecting_host
    from co_scientist.platform.llm.provider_usage import scoped_client

    with scoped_client(
        principal.subject, host=connecting_host(request.client.host if request.client else None)
    ):
        return cast(Response, await call_next(request))


# Register CORS outside ownership so denied responses still carry the
# appropriate CORS headers.
app.add_exception_handler(StorageAdmissionError, storage_error_handler)
app.add_middleware(RequestLimitsMiddleware)
app.add_exception_handler(LaunchPausedError, paused_error_handler)
app.add_middleware(LaunchAdmissionMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Reports and collections are large, repetitive JSON; the run event stream
# (text/event-stream) is excluded by Starlette so events are never held back.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
app.add_middleware(DeploymentAdmissionMiddleware)
# Outermost, so the server span covers every other middleware.
app.add_middleware(TracingMiddleware)


app.include_router(runs_router)
app.include_router(interviews_router)
app.include_router(documents_router)
app.include_router(free_usage_router)
app.include_router(byok_models_router)
app.include_router(logs_router)
app.include_router(spend_router)
app.include_router(usage_router)
app.include_router(feedback_router)
app.include_router(diagnostics_api_router)
app.include_router(launch_control_router)
app.include_router(deployment_control_router)
app.include_router(sentry_alerts_router)


if __name__ == "__main__":
    uvicorn.run(
        "co_scientist.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )

__all__ = [
    "_reclaim_disk_space",
    "_reconcile_and_log_interrupted_runs",
    "_shutdown_recovery",
    "_start_recovery_task",
    "_startup_engine_setup",
]
