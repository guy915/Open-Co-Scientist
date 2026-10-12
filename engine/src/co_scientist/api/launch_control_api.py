from __future__ import annotations

import os
import time
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, FiniteFloat, model_validator

from co_scientist.api.auth import client_id
from co_scientist.api.operator_access import has_admin_token
from co_scientist.core.admission_windows import utc_day
from co_scientist.core.async_bridge import off_loop
from co_scientist.core.config import settings
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.domains.access.free_usage import daily_limit, used_today
from co_scientist.orchestration.repository import events, tasks_lifecycle
from co_scientist.platform.db import (
    Connection,
    connect,
    current_time,
    default_db_path,
    runs,
    transaction,
)
from co_scientist.platform.db.admission import connecting_host
from co_scientist.platform.db.backup_service import read_status
from co_scientist.platform.db.launch_control import (
    RevisionConflictError,
    read_control,
    write_control,
)
from co_scientist.platform.db.models import RunStatus
from co_scientist.platform.db.spend import spending_held
from co_scientist.platform.llm.admission.spend import effective_azure_config
from co_scientist.platform.llm.execution_policy import deployment_routes_are_free
from co_scientist.platform.llm.process_mode import (
    credential_available,
    offline_mode,
    production_routing_enabled,
)
from co_scientist.platform.llm.request.backend import active_backend
from co_scientist.platform.llm.routing import available_slots

router = APIRouter(tags=["launch-operations"])


class ControlUpdate(BaseModel):
    paused: bool
    drain: bool = False
    message: str = Field(default="Research is temporarily paused.", min_length=1, max_length=400)
    resumes_at: FiniteFloat | None = Field(default=None, gt=0)
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def coherent_pause(self) -> ControlUpdate:
        self.message = self.message.strip()
        if not self.message or (not self.paused and (self.drain or self.resumes_at is not None)):
            raise ValueError("Cancellation and a return time require a pause")
        if self.resumes_at is not None and self.resumes_at <= current_time():
            raise ValueError("The expected return time must be in the future")
        return self


def _require_operator(request: Request) -> None:
    if not has_admin_token(request):
        raise HTTPException(status_code=403, detail="Operator token required")


def _credit_snapshot(conn: Connection) -> dict[str, Any]:
    enabled = os.getenv("LLM_AZURE_ENABLED", "").lower() in {"true", "1"}
    if not enabled:
        return {
            "enabled": False,
            "available": False,
            "charged_and_reserved_microeur": None,
            "total_microeur": None,
        }
    charged = int(
        conn.execute("SELECT COALESCE(SUM(charged_microeur),0) FROM llm_spend").fetchone()[0]
    )
    try:
        config = effective_azure_config(conn)
    except ProviderAdmissionError:
        return {
            "enabled": True,
            "available": False,
            "charged_and_reserved_microeur": charged,
            "total_microeur": None,
        }
    held = spending_held(conn)
    forecasts = int(
        conn.execute("SELECT COALESCE(SUM(forecast_microeur),0) FROM llm_routes").fetchone()[0]
    )
    return {
        "enabled": True,
        "available": charged + forecasts < config.total and not held,
        "charged_and_reserved_microeur": charged,
        "total_microeur": config.total,
    }


@lru_cache(maxsize=1)
def _cached_credit(path: str, bucket: int, config: tuple[str, ...]) -> dict[str, Any]:
    # Do not rescan the lifetime money ledger for every visitor poll. Cache at
    # most three seconds, keyed by store and configuration, never researcher ID.
    with connect(path) as conn:
        return _credit_snapshot(conn)


def _free_refusal(
    conn: Connection, owner: str, host: str, now: float
) -> tuple[str | None, float | None, bool]:
    day = utc_day(now)
    reset = float((day + 1) * 86400)
    total, peers, free_total, free_peers = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(host=?),0),COALESCE(SUM(free),0),"
        "COALESCE(SUM(free AND host=?),0) FROM run_admissions WHERE day=?",
        (host, host, day),
    ).fetchone()
    if total >= settings.runs_per_day or peers >= settings.runs_per_host_per_day:
        return "Today's shared run capacity is used up.", reset, False
    active = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(COALESCE(a.host,'unknown')=?),0) FROM runs r "
        "LEFT JOIN run_admissions a ON a.run_id=r.id "
        "WHERE r.status IN ('queued','running','synthesizing')",
        (host,),
    ).fetchone()
    if active[0] >= settings.max_concurrent_runs or active[1] >= settings.concurrent_runs_per_host:
        return "Research capacity is busy while current runs finish.", None, False
    if not offline_mode():
        limit = daily_limit()
        if (
            free_total >= settings.free_runs_globally_per_day
            or free_peers >= settings.free_runs_per_host_per_day
            or (owner and limit is not None and used_today(conn, owner, now) >= limit)
        ):
            return (
                "Today's free research capacity is used up. You can use your own key in Settings.",
                reset,
                True,
            )
        for scope, subject, calls, tokens in (
            (
                "global",
                "",
                settings.app_llm_global_calls_per_day,
                settings.app_llm_global_tokens_per_day,
            ),
            (
                "host",
                host,
                settings.provider_host_calls_per_day,
                settings.provider_host_tokens_per_day,
            ),
            (
                "client",
                owner,
                settings.provider_client_calls_per_day,
                settings.provider_client_tokens_per_day,
            ),
        ):
            row = conn.execute(
                "SELECT calls,tokens FROM provider_admissions "
                "WHERE day=? AND scope=? AND subject=?",
                (day, scope, subject),
            ).fetchone()
            if row and (row[0] >= calls or row[1] >= tokens):
                return (
                    "Today's free model capacity is used up. You can use your own key in Settings.",
                    reset,
                    True,
                )
    return None, None, True


@router.get("/api/launch-status")
@off_loop
def launch_status(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    path = default_db_path() or "./coscientist.db"
    credit = _cached_credit(
        path,
        int(time.monotonic() // 3),
        tuple(
            os.getenv(name, "")
            for name in (
                "LLM_AZURE_ENABLED",
                "LLM_TOTAL_BUDGET_EUR",
                "LLM_AZURE_EXPIRES_AT",
                "LLM_AZURE_CUTOFF_HOURS",
                "LLM_ENABLED",
                "LLM_USD_TO_EUR",
            )
        ),
    )
    with connect() as conn:
        conn.execute("BEGIN")
        control = read_control(conn=conn)
        message, reset, byok_allowed = _free_refusal(
            conn,
            client_id(request),
            connecting_host(request.client.host if request.client else None),
            current_time(),
        )
    reason: str | None = None
    resumes_at: float | None = None
    free_allowed = message is None
    if control.paused:
        reason, message, resumes_at = "paused", control.message, control.resumes_at
    elif message is not None:
        reason, resumes_at = "free_capacity", reset
    elif (
        production_routing_enabled()
        and not offline_mode()
        and getattr(active_backend(), "operator_routing", False)
    ):
        try:
            slots = available_slots(path).slots
        except ProviderAdmissionError:
            slots = ()
        free_allowed = any(slot != "azure" or credit["available"] for slot in slots)
        if not free_allowed:
            reason, message = "credit_exhausted", "No model is available right now"
    elif not offline_mode() and credit["enabled"] and not credit["available"]:
        reason = "credit_exhausted"
        free_allowed = deployment_routes_are_free() and all(
            credential_available(model)
            for model in (settings.model_name, settings.effective_supervisor_model)
            if model
        )
        message = (
            "Azure credit is unavailable. Free routes remain available."
            if free_allowed
            else "Azure credit is unavailable. You can use your own key in Settings."
        )
    return {
        "reason": reason,
        "message": message,
        "resumes_at": resumes_at,
        "paused": control.paused,
        "free_runs_allowed": free_allowed and not control.paused,
        "byok_runs_allowed": byok_allowed and not control.paused,
    }


@router.get("/api/launch-control")
@off_loop
def get_control(request: Request, response: Response) -> dict[str, Any]:
    _require_operator(request)
    response.headers["Cache-Control"] = "no-store"
    with connect() as conn:
        conn.execute("BEGIN")
        enabled = os.getenv("COSCIENTIST_LITESTREAM_ACTIVE") == "1"
        return {
            **asdict(read_control(conn=conn)),
            "credit": _credit_snapshot(conn),
            "backup": {
                "enabled": enabled,
                "verification": read_status(Path(default_db_path() or "./coscientist.db"))
                if enabled
                else None,
            },
        }


@router.put("/api/launch-control")
@off_loop
def update_control(body: ControlUpdate, request: Request, response: Response) -> dict[str, Any]:
    _require_operator(request)
    response.headers["Cache-Control"] = "no-store"
    cancelled = 0
    with transaction(durable=True) as conn:
        was_paused = read_control(conn=conn).paused
        try:
            control = write_control(conn, **body.model_dump())
        except RevisionConflictError as exc:
            raise HTTPException(
                status_code=409, detail="Control changed; reload before updating"
            ) from exc
        if body.paused and body.drain:
            active = conn.execute(
                "SELECT id FROM runs WHERE status IN ('queued','running','synthesizing','paused')"
            ).fetchall()
            for run in active:
                tasks_lifecycle.cancel_run_tasks(run["id"], conn=conn)
                runs.update_run_status(run["id"], RunStatus.CANCELLED, conn=conn)
                events.append_event(run["id"], "status", {"status": "cancelled"}, conn=conn)
            cancelled = len(active)
    if was_paused and not control.paused:
        from co_scientist.orchestration.engine_tasks.inputs import resume_pending_scientist_inputs

        resume_pending_scientist_inputs()
    return {**asdict(control), "cancelled_runs": cancelled}
