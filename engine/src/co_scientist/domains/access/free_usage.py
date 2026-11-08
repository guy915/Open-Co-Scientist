from __future__ import annotations

import sqlite3
from typing import Any

from co_scientist.core import byok_scope
from co_scientist.core.admission_windows import utc_day_bounds
from co_scientist.core.config import settings
from co_scientist.core.run_modes import FREE_TIER_CREDITS, free_tier_credits
from co_scientist.platform.llm.process_mode import offline_mode

# Keyless callers who name no tier start on the cheapest one.
FREE_DEFAULT_TIER = "express"


class FreeUsageExhaustedError(Exception):
    """The daily free credits cannot cover the requested tier."""

    def __init__(self, tier: str, needed: int, remaining: int) -> None:
        super().__init__(tier)
        self.tier = tier
        self.needed = needed
        self.remaining = remaining


def applies(byok: byok_scope.ByokCredential | None, llm_backend: str) -> bool:
    return byok is None and llm_backend == "real"


def daily_limit() -> int | None:
    limit = settings.free_credits_per_day
    return limit if limit > 0 else None


def used_today(conn: sqlite3.Connection, owner: str, now: float) -> int:
    start, _ = utc_day_bounds(now)
    row = conn.execute(
        "SELECT COALESCE(SUM(credits),0) FROM free_run_usage WHERE client_id=? AND created_at>=?",
        (owner, start),
    ).fetchone()
    return int(row[0])


def can_afford_any(used: int) -> bool:
    limit = daily_limit()
    return limit is None or used + min(FREE_TIER_CREDITS.values()) <= limit


def claim_free_run(conn: sqlite3.Connection, owner: str, run_id: str, tier: str) -> None:
    """Count and admission share the create transaction; deleting a run
    never refunds its independent usage-ledger slot.
    """
    from co_scientist.platform.db import current_time

    now = current_time()
    limit = daily_limit()
    cost = free_tier_credits(tier)
    if limit is not None:
        used = used_today(conn, owner, now)
        if used + cost > limit:
            raise FreeUsageExhaustedError(tier, cost, max(limit - used, 0))
    conn.execute(
        "INSERT INTO free_run_usage (run_id, client_id, created_at, credits) VALUES (?,?,?,?)",
        (run_id, owner, now, cost),
    )


def usage_payload(owner: str) -> dict[str, Any]:
    """Offline deterministic execution spends no deployment free-model
    allowance. Amounts are credits; ``costs`` prices each tier.
    """
    from co_scientist.platform.db import connect, current_time

    now = current_time()
    with connect() as conn:
        used = used_today(conn, owner, now)
    limit = daily_limit()
    return {
        "enforced": not offline_mode(),
        "costs": dict(FREE_TIER_CREDITS),
        "limit": limit,
        "used": used,
        "remaining": None if limit is None else max(limit - used, 0),
        "resets_at": utc_day_bounds(now)[1],
    }
