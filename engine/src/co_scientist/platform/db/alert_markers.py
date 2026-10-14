from __future__ import annotations

from co_scientist.platform import db
from co_scientist.platform.telemetry.alert_markers import AlertMarker


class QueueFullError(Exception):
    pass


def enqueue(marker: AlertMarker) -> None:
    # Sentry requires a sub-second acknowledgement. Use a short lock deadline,
    # fsync before acceptance, and retain completed identities beyond replay age.
    with db.connect() as conn:
        conn.execute("PRAGMA busy_timeout=250")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = db.current_time()
            conn.execute("DELETE FROM alert_markers WHERE delivered_at < ?", (now - 8 * 86400,))
            if conn.execute(
                "SELECT 1 FROM alert_markers WHERE id=?", (marker.identity,)
            ).fetchone():
                conn.execute("COMMIT")
                return
            count = conn.execute("SELECT COUNT(*) FROM alert_markers").fetchone()[0]
            if count >= 10000:
                raise QueueFullError
            conn.execute(
                "INSERT INTO alert_markers(id,label,event_time,next_attempt,attempts) "
                "VALUES(?,?,?,?,0)",
                (marker.identity, marker.label, marker.timestamp, now),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def next_pending() -> tuple[AlertMarker, int] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id,label,event_time,attempts FROM alert_markers "
            "WHERE delivered_at IS NULL AND next_attempt<=? ORDER BY next_attempt LIMIT 1",
            (db.current_time(),),
        ).fetchone()
    if row is None:
        return None
    return AlertMarker(row["id"], row["label"], row["event_time"]), row["attempts"]


def finish(identity: str, attempts: int, *, delivered: bool) -> None:
    with db.transaction(durable=True) as conn:
        now = db.current_time()
        conn.execute(
            "UPDATE alert_markers SET delivered_at=?,next_attempt=?,attempts=? WHERE id=?",
            (
                now if delivered else None,
                now + min(3600, 30 * 2 ** min(attempts, 7)),
                attempts + 1,
                identity,
            ),
        )
