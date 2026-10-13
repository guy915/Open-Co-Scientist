from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import connect, current_time, transaction

UNAVAILABLE = "No model is available right now"
# Rates are verified when an allowance is recorded; an old record means
# prices nobody has rechecked.
ALLOWANCE_MAX_AGE_SECONDS = 183 * 86400


@dataclass(frozen=True)
class SpendReservation:
    model: str
    role: str
    amount: int
    total: int
    cutoff: float
    input_bound: int
    output_bound: int
    rates: str
    run_id: str | None = None


@dataclass(frozen=True)
class SpendRecord:
    input_bound: int
    output_bound: int
    rates: str
    settled: bool


@dataclass(frozen=True)
class AzureAllowance:
    version: int
    created_at: float
    allowance: int
    expires_at: float
    cutoff_at: float
    rates: str
    basis: str
    holds_through: int = 0

    def admits(self, now: float) -> bool:
        return now < self.cutoff_at and now - self.created_at <= ALLOWANCE_MAX_AGE_SECONDS


def active_allowance(conn: sqlite3.Connection) -> AzureAllowance | None:
    row = conn.execute("SELECT * FROM llm_azure_allowance ORDER BY version DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return AzureAllowance(
        row["version"],
        row["created_at"],
        row["allowance_microeur"],
        row["expires_at"],
        row["cutoff_at"],
        row["rates"],
        row["basis"],
        row["holds_through"],
    )


def record_allowance(
    conn: sqlite3.Connection,
    *,
    allowance: int,
    expires_at: float,
    cutoff_at: float,
    rates: str,
    basis: str,
    holds_through: int,
) -> int:
    cursor = conn.execute(
        "INSERT INTO llm_azure_allowance "
        "(created_at,allowance_microeur,expires_at,cutoff_at,rates,basis,holds_through) "
        "VALUES (?,?,?,?,?,?,?)",
        (current_time(), allowance, expires_at, cutoff_at, rates, basis, holds_through),
    )
    return int(cursor.lastrowid or 0)


def ledger_total(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COALESCE(SUM(charged_microeur),0) FROM llm_spend").fetchone()[0]
    )


def latest_hold(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(rowid),0) FROM llm_spend_holds").fetchone()[0])


def spending_held(conn: sqlite3.Connection) -> bool:
    # Holds are never deleted; an allowance recorded from fresh provider
    # figures may acknowledge the holds that existed when it was recorded.
    allowance = active_allowance(conn)
    through = allowance.holds_through if allowance is not None else 0
    return latest_hold(conn) > through


def anchored_total(conn: sqlite3.Connection, total: int | None) -> int | None:
    allowance = active_allowance(conn)
    if total is None or allowance is None or not allowance.admits(current_time()):
        return None
    return min(total, allowance.allowance)


def _covers_verified_rates(allowance: AzureAllowance, model: str, rates: str) -> bool:
    # A price or exchange rate below what the operator verified would let the
    # ledger undercount; a higher one is merely more conservative.
    try:
        verified = json.loads(allowance.rates)
        current = json.loads(rates)
        model_rates = verified["models"][model]
        # Without short-tier rates every token pays the long rates verified above.
        return Decimal(current["fx"]) >= Decimal(verified["fx"]) and all(
            Decimal(current[name]) >= Decimal(value)
            for name, value in model_rates.items()
            if name in current or not name.startswith("short_")
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def spend_record(receipt: str, path: str) -> SpendRecord | None:
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM llm_spend WHERE id=?", (receipt,)).fetchone()
    return (
        SpendRecord(row["input_bound"], row["output_bound"], row["rates"], bool(row["settled"]))
        if row
        else None
    )


def hold_spending(receipt: str, path: str) -> None:
    with transaction(path, durable=True) as conn:
        conn.execute("INSERT OR IGNORE INTO llm_spend_holds VALUES (?)", (receipt,))


def reserve_spend(conn: sqlite3.Connection, receipt: str, spend: SpendReservation) -> float:
    now = current_time()
    allowance = active_allowance(conn)
    if (
        allowance is None
        or not allowance.admits(now)
        or not _covers_verified_rates(allowance, spend.model, spend.rates)
    ):
        raise ProviderAdmissionError(UNAVAILABLE)
    total = min(spend.total, allowance.allowance)
    cutoff = min(spend.cutoff, allowance.cutoff_at)
    spent = ledger_total(conn)
    forecasts = conn.execute(
        "SELECT COALESCE(SUM(forecast_microeur),0) FROM llm_routes"
    ).fetchone()[0]
    converted = 0
    if spend.run_id is not None:
        route = conn.execute("SELECT * FROM llm_routes WHERE run_id=?", (spend.run_id,)).fetchone()
        if route is None or not route["azure_allowed"]:
            raise ProviderAdmissionError(UNAVAILABLE)
        converted = min(spend.amount, route["forecast_microeur"])
    if (
        now >= cutoff
        or spend.amount < 0
        or total <= 0
        or spent + forecasts + spend.amount - converted > total
        or spending_held(conn)
    ):
        raise ProviderAdmissionError(UNAVAILABLE)
    if converted:
        conn.execute(
            "UPDATE llm_routes SET forecast_microeur=forecast_microeur-? WHERE run_id=?",
            (converted, spend.run_id),
        )
        conn.execute(
            "INSERT INTO llm_forecast_allocations VALUES (?,?,?)",
            (receipt, spend.run_id, converted),
        )
    conn.execute(
        "INSERT INTO llm_spend "
        "(id,created_at,model,role,reserved_microeur,charged_microeur,"
        "input_bound,output_bound,rates) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            receipt,
            now,
            spend.model,
            spend.role,
            spend.amount,
            spend.amount,
            spend.input_bound,
            spend.output_bound,
            spend.rates,
        ),
    )
    return cutoff


def settle_spend(
    conn: sqlite3.Connection,
    receipt: str,
    charged: int,
    prompt: int,
    output: int,
    cached: int | None,
    written: int | None,
    refused: bool | None = None,
) -> None:
    row = conn.execute("SELECT reserved_microeur FROM llm_spend WHERE id=?", (receipt,)).fetchone()
    if row is None:
        return
    if not 0 <= charged <= row[0]:
        raise ProviderAdmissionError(UNAVAILABLE)
    changed = conn.execute(
        "UPDATE llm_spend SET charged_microeur=?,settled=1,prompt_tokens=?,output_tokens=?,"
        "cached_tokens=?,cache_write_tokens=?,refused=? WHERE id=? AND settled=0",
        (charged, prompt, output, cached, written, refused, receipt),
    ).rowcount
    if changed:
        allocation = conn.execute(
            "SELECT run_id,converted_microeur FROM llm_forecast_allocations WHERE receipt_id=?",
            (receipt,),
        ).fetchone()
        if allocation:
            refund = min(allocation["converted_microeur"], row[0] - charged)
            conn.execute(
                "UPDATE llm_routes SET forecast_microeur=forecast_microeur+? WHERE run_id=?",
                (refund, allocation["run_id"]),
            )
            conn.execute("DELETE FROM llm_forecast_allocations WHERE receipt_id=?", (receipt,))


def hold_after_restore(path: str) -> None:
    hold_spending(f"restored-{uuid.uuid4().hex}", path)


if __name__ == "__main__":
    hold_after_restore(sys.argv[1])
