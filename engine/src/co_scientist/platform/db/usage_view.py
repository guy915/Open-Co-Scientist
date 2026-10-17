from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from co_scientist.platform.db import Connection

_DAY = 86400

# Chats and runs are the durable traces a browser leaves; app_logs is
# row-capped, so it would undercount older days.
_ACTIVITY = (
    "SELECT client_id, created_at, 1 AS chat, 0 AS run FROM interviews "
    "WHERE created_at >= ? AND client_id != '' "
    "UNION ALL "
    "SELECT client_id, created_at, 0, 1 FROM runs "
    "WHERE created_at >= ? AND client_id != '' AND provider != 'mock'"
)


def usage_snapshot(conn: Connection, *, now: float, days: int) -> dict[str, Any]:
    start = (int(now // _DAY) - days + 1) * _DAY
    rows = conn.execute(
        "SELECT CAST(created_at / 86400 AS INTEGER) AS day, COUNT(DISTINCT client_id),"
        " SUM(chat), SUM(run), COUNT(DISTINCT CASE WHEN run = 1 THEN client_id END)"
        f" FROM ({_ACTIVITY}) GROUP BY day ORDER BY day",
        (start, start),
    ).fetchall()
    by_day = {int(day): row for day, *row in rows}
    total = conn.execute(
        "SELECT COUNT(DISTINCT client_id), COALESCE(SUM(chat), 0), COALESCE(SUM(run), 0),"
        f" COUNT(DISTINCT CASE WHEN run = 1 THEN client_id END) FROM ({_ACTIVITY})",
        (start, start),
    ).fetchone()
    first_day = start // _DAY
    daily = []
    for day in range(first_day, first_day + days):
        active, chats, runs, run_clients = by_day.get(day, (0, 0, 0, 0))
        daily.append(
            {
                "date": _iso_day(day),
                "active_clients": int(active),
                "chats_started": int(chats),
                "runs_created": int(runs),
                "run_clients": int(run_clients),
            }
        )
    return {
        "since": _iso_day(first_day),
        "days": days,
        "active_clients": int(total[0]),
        "chats_started": int(total[1]),
        "runs_created": int(total[2]),
        "run_clients": int(total[3]),
        "daily": daily,
    }


def _iso_day(day: int) -> str:
    return datetime.fromtimestamp(day * _DAY, UTC).date().isoformat()
