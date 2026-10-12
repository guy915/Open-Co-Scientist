from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from co_scientist.api.operator_access import is_operator
from co_scientist.core.async_bridge import off_loop
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import connect, current_time, default_db_path
from co_scientist.platform.db.spend import (
    AzureAllowance,
    active_allowance,
    ledger_total,
    spending_held,
)
from co_scientist.platform.db.spend_view import spend_snapshot
from co_scientist.platform.llm.admission.anthropic import cycle_bounds, require_credit_available
from co_scientist.platform.llm.admission.spend import (
    establish_allowance,
    paid_dispatch_config,
    parse_expiry,
)
from co_scientist.platform.llm.routing import available_slots

router = APIRouter(tags=["operations"])


def _configured_money(name: str, default: str = "") -> int | None:
    try:
        value = Decimal(os.getenv(name, default))
        if not value.is_finite() or value <= 0:
            return None
        amount = int((value * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
        return amount if 0 < amount < 2**63 else None
    except InvalidOperation:
        return None


@router.get("/api/spend")
@off_loop
def get_spend(request: Request, response: Response) -> dict[str, Any]:
    if not is_operator(request):
        raise HTTPException(404, "not found")
    response.headers["Cache-Control"] = "no-store"
    now = current_time()
    start: float | None
    end: float | None
    try:
        start, end = cycle_bounds(now, int(os.getenv("ANTHROPIC_BILLING_RESET_DAY", "7")))
    except (ValueError, ProviderAdmissionError):
        start, end = None, None
    configured = _configured_money("LLM_TOTAL_BUDGET_EUR")
    with connect() as conn:
        conn.execute("BEGIN")
        allowance = active_allowance(conn)
        snapshot = spend_snapshot(
            conn,
            now=now,
            # Without an operator record the store refuses Azure entirely.
            total_microeur=min(configured, allowance.allowance)
            if configured is not None and allowance is not None
            else None,
            credit_microusd=_configured_money("ANTHROPIC_MONTHLY_CREDIT_USD", "100")
            if start is not None
            else None,
            cycle_start=start,
            cycle_end=end,
        )
        path = str(conn.execute("PRAGMA database_list").fetchone()[2])
        azure_held = spending_held(conn)
    try:
        slots = available_slots(path).slots
    except ProviderAdmissionError:
        slots = ()
    for key, available in (
        ("azure", paid_dispatch_config),
        ("anthropic", require_credit_available),
    ):
        try:
            available(path)
            snapshot[key]["available"] = bool(
                key in slots
                and (
                    key != "azure" or (not azure_held and (snapshot[key]["remaining_eur"] or 0) > 0)
                )
            )
        except ProviderAdmissionError:
            snapshot[key]["available"] = False
    snapshot["azure"]["allowance_version"] = allowance.version if allowance else None
    snapshot["azure"]["cutoff_at"] = _instant(allowance.cutoff_at) if allowance else None
    return snapshot


def _instant(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _allowance_view(allowance: AzureAllowance) -> dict[str, Any]:
    return {
        "version": allowance.version,
        "allowance_eur": str(Decimal(allowance.allowance) / 1_000_000),
        "expires_at": _instant(allowance.expires_at),
        "cutoff_at": _instant(allowance.cutoff_at),
        "rates": json.loads(allowance.rates),
        "basis": json.loads(allowance.basis),
        "holds_through": allowance.holds_through,
    }


class AllowanceRecord(BaseModel):
    grant_eur: Decimal
    prior_usage_eur: Decimal
    buffer_eur: Decimal
    expires_at: str = Field(max_length=64)
    cutoff_hours: Decimal = Decimal(48)
    usd_to_eur: Decimal
    supersedes_version: int | None = None
    acknowledge_holds: bool = False
    note: str = Field(default="", max_length=500)


@router.get("/api/spend/azure-allowance")
@off_loop
def get_azure_allowance(request: Request, response: Response) -> dict[str, Any]:
    if not is_operator(request):
        raise HTTPException(404, "not found")
    response.headers["Cache-Control"] = "no-store"
    with connect() as conn:
        conn.execute("BEGIN")
        allowance = active_allowance(conn)
        ledger = ledger_total(conn)
    return {
        "active": _allowance_view(allowance) if allowance else None,
        "ledger_charged_and_reserved_eur": f"{Decimal(ledger) / 1_000_000:.6f}",
    }


@router.post("/api/spend/azure-allowance")
@off_loop
def post_azure_allowance(
    body: AllowanceRecord, request: Request, response: Response
) -> dict[str, Any]:
    # A new version never forgets ledger spend; raising the allowance or
    # extending expiry also needs the matching settings changed.
    if not is_operator(request):
        raise HTTPException(404, "not found")
    response.headers["Cache-Control"] = "no-store"
    try:
        expires_at = parse_expiry(body.expires_at)
    except ProviderAdmissionError as error:
        raise HTTPException(422, "expires_at must be an ISO 8601 instant with an offset") from error
    path = default_db_path() or "./coscientist.db"
    try:
        recorded = establish_allowance(
            path,
            grant_eur=body.grant_eur,
            prior_usage_eur=body.prior_usage_eur,
            buffer_eur=body.buffer_eur,
            expires_at=expires_at,
            cutoff_hours=body.cutoff_hours,
            usd_to_eur=body.usd_to_eur,
            supersedes_version=body.supersedes_version,
            acknowledge_holds=body.acknowledge_holds,
            note=body.note,
        )
    except (ValueError, ProviderAdmissionError) as error:
        raise HTTPException(422, str(error) or "allowance refused") from error
    return _allowance_view(recorded)
