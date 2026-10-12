from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal, cast

from co_scientist.core.admission_windows import utc_day, utc_day_bounds
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import connect, current_time, transaction
from co_scientist.platform.db.spend import (
    UNAVAILABLE,
    anchored_total,
    ledger_total,
    spending_held,
)

Slot = Literal["openrouter", "anthropic", "azure"]


class OpenRouterCapacityError(ProviderAdmissionError):
    pass


@dataclass(frozen=True)
class RunRoute:
    slot: Slot
    azure_allowed: bool
    forecast: int


def remaining_total(conn: sqlite3.Connection, total: int) -> int:
    limit = anchored_total(conn, total)
    if limit is None:
        return 0
    spent = ledger_total(conn)
    forecasts = conn.execute(
        "SELECT COALESCE(SUM(forecast_microeur),0) FROM llm_routes"
    ).fetchone()[0]
    return max(0, limit - spent - int(forecasts))


def admit_route(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    slots: tuple[Slot, ...],
    estimate: int,
    total: int | None,
) -> RunRoute:
    run = conn.execute("SELECT profile FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or run[0] != "express":
        raise ProviderAdmissionError(UNAVAILABLE)
    allowed = (
        "azure" in slots
        and total is not None
        and 0 < estimate <= remaining_total(conn, total)
        and not spending_held(conn)
    )
    choices = tuple(slot for slot in slots if slot != "azure" or allowed)
    if not choices:
        raise ProviderAdmissionError(UNAVAILABLE)
    route = RunRoute(choices[0], allowed, estimate if allowed else 0)
    conn.execute(
        "INSERT INTO llm_routes (run_id,slot,azure_allowed,forecast_microeur) VALUES (?,?,?,?)",
        (run_id, route.slot, int(route.azure_allowed), route.forecast),
    )
    _provenance(conn, run_id, "none", route.slot, "admission", "worker")
    return route


def route_for_run(run_id: str, path: str) -> RunRoute | None:
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM llm_routes WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    return RunRoute(cast(Slot, row["slot"]), bool(row["azure_allowed"]), row["forecast_microeur"])


def release_forecast(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("UPDATE llm_routes SET forecast_microeur=0 WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM llm_forecast_allocations WHERE run_id=?", (run_id,))


def reacquire_forecast(
    conn: sqlite3.Connection, run_id: str, *, total: int | None, estimate: int
) -> None:
    row = conn.execute("SELECT * FROM llm_routes WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return
    if total is None or spending_held(conn):
        allowed = False
    else:
        allowed = 0 < estimate <= remaining_total(conn, total) + row["forecast_microeur"]
    conn.execute(
        "UPDATE llm_routes SET forecast_microeur=?,azure_allowed=? WHERE run_id=?",
        (estimate if allowed else 0, int(allowed), run_id),
    )


def free_available(path: str, ceiling: int) -> bool:
    now = current_time()
    with connect(path) as conn:
        blocked = conn.execute(
            "SELECT unavailable_until FROM llm_route_blocks WHERE slot='openrouter'"
        ).fetchone()
        row = conn.execute(
            "SELECT calls FROM llm_free_calls WHERE day=?", (utc_day(now),)
        ).fetchone()
    return not (blocked and now < blocked[0]) and (row is None or row[0] < ceiling)


def reserve_free_call(conn: sqlite3.Connection, ceiling: int) -> None:
    now = current_time()
    day = utc_day(now)
    blocked = conn.execute(
        "SELECT unavailable_until FROM llm_route_blocks WHERE slot='openrouter'"
    ).fetchone()
    row = conn.execute("SELECT calls FROM llm_free_calls WHERE day=?", (day,)).fetchone()
    if ceiling <= 0 or (blocked and now < blocked[0]) or (row is not None and row[0] >= ceiling):
        raise OpenRouterCapacityError(UNAVAILABLE)
    conn.execute(
        "INSERT INTO llm_free_calls (day,calls) VALUES (?,1) "
        "ON CONFLICT(day) DO UPDATE SET calls=calls+1",
        (day,),
    )
    conn.execute("DELETE FROM llm_free_calls WHERE day<?", (day - 7,))


def exhaust_free_routes(path: str) -> None:
    with transaction(path) as conn:
        conn.execute(
            "INSERT INTO llm_route_blocks (slot,unavailable_until) VALUES ('openrouter',?) "
            "ON CONFLICT(slot) DO UPDATE SET unavailable_until=excluded.unavailable_until",
            (utc_day_bounds(current_time())[1],),
        )


def record_switch(
    path: str,
    run_id: str,
    previous: Slot,
    selected: Slot,
    reason: str,
    role: str,
    *,
    sticky: bool,
) -> None:
    with transaction(path, durable=selected == "azure") as conn:
        if not conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
            raise ProviderAdmissionError(UNAVAILABLE)
        if sticky:
            conn.execute("UPDATE llm_routes SET slot=? WHERE run_id=?", (selected, run_id))
        _provenance(conn, run_id, previous, selected, reason, role)


def _provenance(
    conn: sqlite3.Connection, run_id: str, previous: str, selected: str, reason: str, role: str
) -> None:
    # Selection and provenance must commit together. This store layer cannot
    # import the orchestration event writer above it.
    payload = json.dumps(
        {"from": previous, "to": selected, "reason": reason, "role": role, "activity": "other"}
    )
    conn.execute(
        "INSERT INTO run_events (run_id,seq,type,payload_json,created_at) "
        "VALUES (?,1+MAX((SELECT COALESCE(MAX(seq),0) FROM run_events WHERE run_id=?),"
        "(SELECT COALESCE(MAX(last_event_seq),0) FROM checkpoints WHERE run_id=?)),"
        "'model_provider',?,?)",
        (run_id, run_id, run_id, payload, current_time()),
    )
