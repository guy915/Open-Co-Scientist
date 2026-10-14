from __future__ import annotations

import sqlite3
from typing import Any

from co_scientist.core import byok_scope
from co_scientist.core.admission_windows import utc_day_bounds
from co_scientist.core.config import settings
from co_scientist.platform.llm.process_mode import offline_mode

FREE_TIER = "express"


class FreeUsageExhaustedError(Exception):
    """The daily free-run allowance is exhausted."""


def applies(byok: byok_scope.ByokCredential | None, llm_backend: str) -> bool:
    return byok is None and llm_backend == "real"


def daily_limit() -> int | None:
    limit = settings.free_runs_per_day
    return limit if limit > 0 else None


def used_today(conn: sqlite3.Connection, owner: str, now: float) -> int:
    start, _ = utc_day_bounds(now)
    row = conn.execute(
        "SELECT COUNT(*) FROM free_run_usage WHERE client_id=? AND created_at>=?",
        (owner, start),
    ).fetchone()
    return int(row[0])


def claim_free_run(conn: sqlite3.Connection, owner: str, run_id: str) -> None:
    """Count and admission share the create transaction; deleting a run
    never refunds its independent usage-ledger slot.
    """
    from co_scientist.platform.db import current_time

    now = current_time()
    limit = daily_limit()
    if limit is not None and used_today(conn, owner, now) >= limit:
        raise FreeUsageExhaustedError
    conn.execute(
        "INSERT INTO free_run_usage (run_id, client_id, created_at) VALUES (?,?,?)",
        (run_id, owner, now),
    )


def usage_payload(owner: str) -> dict[str, Any]:
    """Offline deterministic execution spends no deployment free-model
    allowance.
    """
    from co_scientist.platform.db import connect, current_time

    now = current_time()
    with connect() as conn:
        used = used_today(conn, owner, now)
    limit = daily_limit()
    return {
        "enforced": not offline_mode(),
        "tier": FREE_TIER,
        "limit": limit,
        "used": used,
        "remaining": None if limit is None else max(limit - used, 0),
        "resets_at": utc_day_bounds(now)[1],
    }
