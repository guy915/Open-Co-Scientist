import sqlite3
import time

MAX_GLOBAL_REQUESTS = 240
MAX_GLOBAL_RECORDS = 2000
MAX_GLOBAL_BYTES = 4 * 1024**2


def claim(conn: sqlite3.Connection, records: int, byte_size: int) -> bool:
    minute = int(time.time() // 60)
    conn.execute("DELETE FROM log_ingest_admissions WHERE minute < ?", (minute - 2880,))
    row = conn.execute(
        "SELECT requests, records, bytes FROM log_ingest_admissions WHERE minute=?", (minute,)
    ).fetchone()
    requests, used_records, used_bytes = tuple(row) if row is not None else (0, 0, 0)
    if (
        requests + 1 > MAX_GLOBAL_REQUESTS
        or used_records + records > MAX_GLOBAL_RECORDS
        or used_bytes + byte_size > MAX_GLOBAL_BYTES
    ):
        return False
    conn.execute(
        "INSERT INTO log_ingest_admissions VALUES (?,?,?,?) ON CONFLICT(minute) "
        "DO UPDATE SET requests=excluded.requests, records=excluded.records, "
        "bytes=excluded.bytes",
        (minute, requests + 1, used_records + records, used_bytes + byte_size),
    )
    return True
