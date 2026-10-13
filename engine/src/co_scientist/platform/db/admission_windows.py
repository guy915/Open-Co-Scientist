from __future__ import annotations

from co_scientist.core.admission_windows import WindowCapacity, check_capacity
from co_scientist.platform.db import Connection


def reserve_feedback_window(
    conn: Connection,
    owner: str,
    host: str,
    *,
    now: float,
    owner_limit: int,
    host_limit: int,
    global_limit: int,
    detail: str,
) -> None:
    # The caller owns the writer transaction through admission and insertion.
    conn.execute("DELETE FROM feedback_admissions WHERE created_at<=?", (now - 60,))
    total, owned, hosted, first, owner_first, host_first = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(client_id=?),0),COALESCE(SUM(host_key=?),0),"
        "MIN(created_at),MIN(CASE WHEN client_id=? THEN created_at END),"
        "MIN(CASE WHEN host_key=? THEN created_at END) FROM feedback_admissions",
        (owner, host, owner, host),
    ).fetchone()
    check_capacity(
        [
            WindowCapacity(total, global_limit, (now if first is None else first) + 60),
            WindowCapacity(owned, owner_limit, (now if owner_first is None else owner_first) + 60),
            WindowCapacity(hosted, host_limit, (now if host_first is None else host_first) + 60),
        ],
        now=now,
        detail=detail,
    )
    conn.execute("INSERT INTO feedback_admissions VALUES (?,?,?)", (owner, host, now))
