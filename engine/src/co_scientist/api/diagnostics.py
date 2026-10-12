from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any

from co_scientist.core.config import any_provider_credential, settings
from co_scientist.orchestration.engine_adapter import engine_importable
from co_scientist.orchestration.repository.tasks import queue_health_snapshot
from co_scientist.platform import db
from co_scientist.platform.db import default_db_path

logger = logging.getLogger(__name__)

HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"

# Probe errors mean unknown availability, distinct from definitive down answers.
PROBE_UP = "up"
PROBE_DOWN = "down"
PROBE_ERROR = "error"


@dataclass
class HealthCheck:
    ok: bool
    detail: str | None = None


def check_store(db_path: str | None = None) -> HealthCheck:
    try:
        with db.connect(db_path) as conn:
            conn.execute("SELECT 1").fetchone()
        return HealthCheck(ok=True)
    except Exception as exc:
        logger.error("Store health check failed: %s", exc)
        return HealthCheck(ok=False, detail=f"{type(exc).__name__}: {exc}")


def check_engine() -> HealthCheck:
    if engine_importable():
        return HealthCheck(ok=True)
    return HealthCheck(ok=False, detail="co_scientist package not importable")


def derive_health_status(store_check: HealthCheck, engine_check: HealthCheck) -> str:
    """Only store unreachability proves the process cannot serve; engine
    diagnostics must not cause a restart spiral.
    """
    if not store_check.ok:
        return UNHEALTHY
    if any_provider_credential() and not engine_check.ok:
        return DEGRADED
    return HEALTHY


def check_queue(db_path: str | None = None) -> HealthCheck:
    """Queue depth is normal load; only stalled runs without recoverable
    work indicate lost progress.
    """
    try:
        snapshot = queue_health_snapshot(db_path=db_path)
    except Exception as exc:
        logger.error("Queue health check failed: %s", exc)
        return HealthCheck(ok=False, detail=f"{type(exc).__name__}: {exc}")
    if not snapshot.stalled_run_ids:
        return HealthCheck(ok=True)
    ids = ", ".join(snapshot.stalled_run_ids[:5])
    return HealthCheck(
        ok=False,
        detail=(f"{len(snapshot.stalled_run_ids)} run(s) stalled with no claimable work: {ids}"),
    )


def check_disk(
    db_path: str | None = None,
    *,
    min_free_bytes: int | None = None,
) -> HealthCheck:
    """Disk pressure degrades health: restarting frees no space and would
    interrupt still-usable reads.
    """
    threshold = (
        settings.health_check_min_free_disk_bytes if min_free_bytes is None else min_free_bytes
    )
    target = db_path or default_db_path() or "./coscientist.db"
    directory = os.path.dirname(os.path.abspath(target)) or "."
    try:
        free = shutil.disk_usage(directory).free
    except Exception as exc:
        logger.error("Disk health check failed: %s", exc)
        return HealthCheck(ok=False, detail=f"{type(exc).__name__}: {exc}")
    if free < threshold:
        return HealthCheck(
            ok=False,
            detail=f"{free} bytes free on {directory} (floor {threshold})",
        )
    return HealthCheck(ok=True)


def derive_overall_health(
    store_check: HealthCheck,
    engine_check: HealthCheck,
    queue_check: HealthCheck,
    disk_check: HealthCheck,
) -> str:
    """Queue or disk degradation cannot become failed liveness; deploy
    healthchecks would kill the container mid-run.
    """
    base = derive_health_status(store_check, engine_check)
    if base == UNHEALTHY:
        return UNHEALTHY
    if not queue_check.ok or not disk_check.ok:
        return DEGRADED
    return base


# Short local snapshot caching bounds continuously polled queries rather than
# hiding network latency.
_health_check_cache: tuple[float, tuple[HealthCheck, HealthCheck]] | None = None


def queue_and_disk_health_cached(
    db_path: str | None = None,
) -> tuple[HealthCheck, HealthCheck]:
    global _health_check_cache
    now = time.monotonic()
    if _health_check_cache is not None and now < _health_check_cache[0]:
        return _health_check_cache[1]
    result = (check_queue(db_path), check_disk(db_path))
    ttl = settings.health_check_cache_ttl_seconds
    _health_check_cache = (now + ttl, result)
    return result


@dataclass
class ProbeResult:
    """Down is a definitive probe answer; error means availability remains
    unknown.
    """

    available: bool
    state: str
    error: str | None = None


def _probe_error(detail: str) -> ProbeResult:
    return ProbeResult(available=False, state=PROBE_ERROR, error=detail)


async def _run_probe(coro: Any, timeout: float) -> ProbeResult:
    """Timeouts and probe failures mean unknown availability, never a
    definitive down verdict.
    """
    try:
        available = bool(await asyncio.wait_for(coro, timeout=timeout))
        return ProbeResult(available=available, state=PROBE_UP if available else PROBE_DOWN)
    except asyncio.TimeoutError:
        # On Python 3.10 asyncio.TimeoutError is not yet the built-in
        # TimeoutError alias.
        return _probe_error(f"probe timed out after {timeout:g}s")
    except Exception as exc:
        return _probe_error(f"{type(exc).__name__}: {exc}")


async def _probe_literature_stack() -> tuple[ProbeResult, ProbeResult, ProbeResult]:
    """Probe actual provider reachability, not tool registration; a refused
    key can leave a registered tool unable to search.
    """
    try:
        from co_scientist.platform.retrieval.mcp_client import (
            check_literature_source_available,
            check_mcp_available,
            check_web_search_available,
        )
    except Exception as exc:
        unavailable = _probe_error(f"engine unavailable: {exc}")
        return unavailable, unavailable, unavailable

    timeout = settings.status_probe_timeout_seconds
    return await asyncio.gather(
        _run_probe(check_mcp_available(), timeout),
        _run_probe(check_literature_source_available(), timeout),
        _run_probe(check_web_search_available(), timeout),
    )


_ProbeTriple = tuple[ProbeResult, ProbeResult, ProbeResult]
_probe_cache: tuple[float, _ProbeTriple] | None = None
# One refresh at a time, bound to the loop that started it; a task must not
# be awaited from another event loop.
_probe_refresh: tuple[asyncio.AbstractEventLoop, asyncio.Task[_ProbeTriple]] | None = None
# Status polls arrive less often than the TTL, so without a stale window
# every poll would wait on live probes (p50 about 380 ms in production).
_STALE_SERVE_SECONDS = 300.0


async def _refresh_probe_cache() -> _ProbeTriple:
    global _probe_cache
    results = await _probe_literature_stack()
    _probe_cache = (time.monotonic() + settings.status_probe_cache_ttl_seconds, results)
    return results


def _probe_refresh_task() -> asyncio.Task[_ProbeTriple]:
    global _probe_refresh
    loop = asyncio.get_running_loop()
    if _probe_refresh is not None:
        refresh_loop, task = _probe_refresh
        if refresh_loop is loop and not task.done():
            return task
    task = loop.create_task(_refresh_probe_cache())
    _probe_refresh = (loop, task)
    return task


async def probe_literature_stack_cached() -> _ProbeTriple:
    """Expired results are served while one background refresh runs, so
    status polls never wait on probes unless the cache is empty or old.
    """
    cached = _probe_cache
    now = time.monotonic()
    if cached is not None and now < cached[0]:
        return cached[1]
    refresh = _probe_refresh_task()
    if cached is not None and now < cached[0] + _STALE_SERVE_SECONDS:
        return cached[1]
    return await asyncio.shield(refresh)
