from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from co_scientist.platform.db import Connection


def spend_snapshot(
    conn: Connection,
    *,
    now: float,
    total_microeur: int | None,
    credit_microusd: int | None,
    cycle_start: float | None,
    cycle_end: float | None,
) -> dict[str, Any]:
    # One read snapshot keeps the displayed remainder consistent with totals.
    day = int(now // 86400) * 86400
    week_start = day - datetime.fromtimestamp(now, timezone.utc).weekday() * 86400
    today, week, total, unknown, last_seven = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN settled=1 AND created_at>=? THEN charged_microeur END),0),"
        "COALESCE(SUM(CASE WHEN settled=1 AND created_at>=? THEN charged_microeur END),0),"
        "COALESCE(SUM(CASE WHEN settled=1 THEN charged_microeur END),0),"
        "COALESCE(SUM(CASE WHEN settled=0 THEN charged_microeur END),0),"
        "COALESCE(SUM(CASE WHEN settled=1 AND created_at>=? THEN charged_microeur END),0) "
        "FROM llm_spend",
        (day, week_start, now - 7 * 86400),
    ).fetchone()
    forecast = conn.execute("SELECT COALESCE(SUM(forecast_microeur),0) FROM llm_routes").fetchone()[
        0
    ]
    remaining = (
        max(0, total_microeur - total - unknown - forecast) if total_microeur is not None else None
    )
    burn = last_seven / 7
    spent, held = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN cycle_start=? AND settled=1 THEN charged_microusd END),0),"
        "COALESCE(SUM(CASE WHEN settled=0 THEN charged_microusd END),0) FROM anthropic_credit",
        (cycle_start,),
    ).fetchone()
    credit_left = max(0, credit_microusd - spent - held) if credit_microusd is not None else None
    cache: list[dict[str, Any]] = []
    for table, currency, column in (
        ("llm_spend", "EUR", "charged_microeur"),
        ("anthropic_credit", "USD", "charged_microusd"),
    ):
        cache.extend(
            {
                "currency": currency,
                "role": row[0],
                "calls": row[1],
                "prompt_tokens": row[2],
                "cache_read_tokens": row[3],
                "cache_write_tokens": row[4],
                "cost": row[5] / 1_000_000,
                "refusal_measured_calls": row[6],
                "refusals": row[7],
            }
            for row in conn.execute(
                f"SELECT role,COUNT(*),SUM(prompt_tokens),SUM(COALESCE(cached_tokens,0)),"
                f"SUM(COALESCE(cache_write_tokens,0)),SUM({column}),COUNT(refused),"
                f"COALESCE(SUM(refused),0) "
                f"FROM {table} WHERE settled=1 GROUP BY role ORDER BY role"
            )
        )
    return {
        "azure": {
            "today_eur": today / 1_000_000,
            "week_eur": week / 1_000_000,
            "total_spent_eur": total / 1_000_000,
            "reserved_eur": unknown / 1_000_000,
            "run_forecasts_eur": forecast / 1_000_000,
            "total_budget_eur": total_microeur / 1_000_000 if total_microeur is not None else None,
            "remaining_eur": remaining / 1_000_000 if remaining is not None else None,
            "burn_eur_per_day": burn / 1_000_000,
            "days_at_current_rate": remaining / burn
            if remaining is not None and burn > 0
            else None,
        },
        "anthropic": {
            "cycle_spent_usd": spent / 1_000_000,
            "reserved_usd": held / 1_000_000,
            "grant_usd": credit_microusd / 1_000_000 if credit_microusd is not None else None,
            "remaining_credit_usd": credit_left / 1_000_000 if credit_left is not None else None,
            "usable_allowance_usd": max(0, credit_microusd * 95 // 100 - spent - held) / 1_000_000
            if credit_microusd is not None
            else None,
            "reset_at": cycle_end,
        },
        "cache_by_role": cache,
    }
