from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from co_scientist.api.auth import client_id
from co_scientist.api.operator_access import is_operator
from co_scientist.core.admission_windows import WindowExceededError
from co_scientist.core.async_bridge import off_loop
from co_scientist.core.config import settings
from co_scientist.domains.access.admission import reserve_memory_window
from co_scientist.platform import db
from co_scientist.platform.db import Connection, log_admission, logs
from co_scientist.platform.db.logs import LogFilters, NewLogRecord
from co_scientist.platform.telemetry.logging_setup import level_to_number

# Process-local limit; assumes one API replica and needs shared storage when
# replicated. Ingestion runs on the handler thread pool, so the window needs a
# lock: expiring stale scopes mutates the map it reads.
_ingest_hits: dict[str, list[float]] = {}
_ingest_hits_lock = threading.Lock()


def _rate_limit_keys(request: Request) -> tuple[str, str]:
    """Caller-controlled compatibility IDs can rotate freely; independently
    enforce connecting-host and verified identity budgets.
    """
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}", f"id:{client_id(request) or 'anonymous'}"


def _check_both_rates(
    hits_by_scope: dict[str, list[float]], request: Request, limit: int, detail: str
) -> None:
    try:
        reserve_memory_window(
            hits_by_scope,
            _rate_limit_keys(request),
            lock=_ingest_hits_lock,
            limit=limit,
            now=time.monotonic(),
            detail=detail,
        )
    except WindowExceededError as exc:
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)}
        ) from exc


def _check_ingest_rate(request: Request) -> None:
    _check_both_rates(
        _ingest_hits,
        request,
        settings.logs_ingest_per_minute,
        "log ingestion rate exceeded",
    )


router = APIRouter(tags=["logs"])

MAX_CLIENT_BATCH = 50
MAX_CLIENT_MESSAGE_CHARS = 2000


def _scope_for(request: Request) -> str | None:
    """Unidentified remote callers match no records; only operator access
    permits the app-wide view.
    """
    if is_operator(request):
        return None
    # Unidentified remote callers match no records, never the operator-wide
    # scope.
    return client_id(request) or "\x00-anonymous"


def _sanitize(text: str) -> str:
    # Open ingestion must not let control characters forge diagnostic entries.
    return "".join(ch if ch.isprintable() else " " for ch in text).strip()


def _min_levelno(min_level: str | None) -> int:
    if min_level is None:
        return 0
    levelno = level_to_number(min_level)
    if levelno is None:
        raise HTTPException(status_code=422, detail=f"unknown log level: {min_level}")
    return levelno


class LogQuery(BaseModel):
    """Query parameters of ``GET /api/logs``; the caller's scope is derived
    from the request, never accepted from it.
    """

    after_id: int = Field(0, ge=0)
    limit: int = Field(200, ge=1, le=1000)
    min_level: str | None = None


def _query_logs_payload(
    conn: Connection,
    filters: LogFilters,
    limit: int,
) -> dict[str, Any]:
    rows = logs.list_logs(filters=filters, limit=limit, conn=conn)
    # Count the full matching set so bounded display windows do not understate
    # totals.
    total = logs.count_logs(filters=dataclasses.replace(filters, after_id=0), conn=conn)
    # Count rows after the session anchor; subtracting snapshots breaks when
    # retention or scoped clears remove older rows.
    session_total = logs.count_logs(filters=filters, conn=conn)
    last_id = logs.latest_log_id(conn=conn)
    return {
        "logs": rows,
        "last_id": last_id,
        "total": total,
        "session_total": session_total,
    }


def logs_payload(query: LogQuery, *, scope_client_id: str | None = None) -> dict[str, Any]:
    filters = LogFilters(
        after_id=query.after_id,
        min_levelno=_min_levelno(query.min_level),
        scope_client_id=scope_client_id,
    )
    with db.connect() as conn:
        return _query_logs_payload(conn, filters, query.limit)


class ClientLogRecord(BaseModel):
    """One log record submitted by the frontend."""

    message: str
    level: str = "info"
    logger: str = "ui"
    run_id: str | None = None


class ClientLogBatch(BaseModel):
    """A batch of frontend log records."""

    records: list[ClientLogRecord] = Field(..., min_length=1, max_length=MAX_CLIENT_BATCH)


@router.post("/api/logs")
@off_loop
def post_logs(batch: ClientLogBatch, request: Request) -> dict[str, Any]:
    """Ingest frontend log records into the persisted app-wide log.

    Records are namespaced under the ``ui.`` logger prefix so their origin
    stays obvious next to backend records; unknown level names fall back
    to INFO and messages are truncated to a sane length.
    """
    owner = client_id(request)
    prepared = []
    for record in batch.records:
        logger_name = _sanitize(record.logger)
        if logger_name != "ui" and not logger_name.startswith("ui."):
            logger_name = f"ui.{logger_name}"
        logger_name = logger_name.encode()[:128].decode("utf-8", errors="ignore")
        message = (
            _sanitize(record.message)
            .encode()[:MAX_CLIENT_MESSAGE_CHARS]
            .decode("utf-8", errors="ignore")
        )
        prepared.append((record, logger_name, message))
    byte_size = sum(len(logger.encode()) + len(message.encode()) for _, logger, message in prepared)
    # Batch ingestion acquires SQLite's writer once, not once per submitted
    # record.
    with db.transaction() as conn:
        for record in batch.records:
            if record.run_id is not None and (
                not owner
                or conn.execute(
                    "SELECT 1 FROM runs WHERE id=? AND client_id=?",
                    (record.run_id, owner),
                ).fetchone()
                is None
            ):
                raise HTTPException(status_code=404, detail="run not found")
        if not log_admission.claim(conn, len(prepared), byte_size):
            raise HTTPException(status_code=429, detail="global log ingestion budget reached")
        # Rejected global traffic cannot grow the process-local scope map.
        _check_ingest_rate(request)
        for record, logger_name, message in prepared:
            levelno = level_to_number(record.level) or logging.INFO
            logs.append_log(
                NewLogRecord(
                    level=logging.getLevelName(levelno),
                    levelno=levelno,
                    logger_name=logger_name,
                    message=message,
                    run_id=record.run_id,
                    client_id=owner or None,
                ),
                conn=conn,
            )
        logs.prune_ui_logs(conn=conn)
        last_id = logs.latest_log_id(conn=conn)
    return {"added": len(batch.records), "last_id": last_id}


@router.get("/api/logs")
@off_loop
def get_logs(
    request: Request,
    query: Annotated[LogQuery, Query()],
) -> dict[str, Any]:
    """Return persisted application log records, oldest-first.

    Operators (loopback callers, or holders of ``LOGS_ADMIN_TOKEN`` via
    the ``X-Logs-Token`` header) get the app-wide view. Every other
    caller sees only their own records: those they submitted and those
    belonging to runs they own.
    """
    return logs_payload(query, scope_client_id=_scope_for(request))


__all__ = ["_check_ingest_rate"]
