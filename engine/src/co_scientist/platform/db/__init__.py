from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from co_scientist.platform.db.schema import ADDED_COLUMNS as _ADDED_COLUMNS
from co_scientist.platform.db.schema import SCHEMA as _SCHEMA

# Layers above the store name these instead of importing sqlite3.
Connection = sqlite3.Connection
Error = sqlite3.Error

logger = logging.getLogger(__name__)


_lock = threading.RLock()
_initialized: set[str] = set()


def current_time() -> float:
    return time.time()


def default_db_path() -> str | None:
    return os.getenv("COSCIENTIST_DB_PATH") or None


def _resolved_db_path(path: str | None = None) -> str:
    # Resolve the path at call time so explicit environment overrides apply
    # without reimporting this module.
    return path or default_db_path() or "./coscientist.db"


def _open_raw_connection(db_path: str) -> sqlite3.Connection:
    # Once initialized, hot reads need no parent-directory syscall.
    if db_path not in _initialized:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # Autocommit permits explicit transaction ownership; connections can cross
    # the asynchronous caller's thread boundary.
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Rows behave like dicts: row["col"].
    # WAL NORMAL avoids per-commit fsync saturation; checkpoints sync
    # durability, with recent transactions vulnerable only to OS failure.
    conn.execute("PRAGMA synchronous=NORMAL")
    # Foreign-key enforcement is per connection, not file-wide; otherwise
    # deletes silently leave orphan rows.
    conn.execute("PRAGMA foreign_keys=ON")
    # Erased payloads must not remain in free pages of later backup snapshots.
    conn.execute("PRAGMA secure_delete=ON")
    if os.getenv("COSCIENTIST_LITESTREAM_ACTIVE") == "1":
        conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def _ensure_schema_initialized(db_path: str, conn: sqlite3.Connection) -> None:
    """Serialize first-use schema setup, but keep initialized hot paths free
    of the setup lock.
    """
    # Serialize initial schema setup without locking every initialized
    # connection.
    if db_path not in _initialized:
        with _lock:
            if db_path not in _initialized:
                _init_schema(conn)
                _initialized.add(db_path)


@contextlib.contextmanager
def connect(
    path: str | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    db_path = _resolved_db_path(path)
    conn = _open_raw_connection(db_path)
    try:
        _ensure_schema_initialized(db_path, conn)
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction(
    path: str | None = None,
    *,
    durable: bool = False,
) -> Generator[sqlite3.Connection, None, None]:
    """Batch related writes under one commit and fsync; never hold this
    writer lock over network I/O.
    """
    with connect(path) as conn:
        if durable:
            # Paid dispatch must not outrun an unsynced money reservation.
            conn.execute("PRAGMA synchronous=FULL")
        # Acquire the writer at BEGIN to avoid a mid-transaction reader-to-
        # writer upgrade deadlock.
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


@contextlib.contextmanager
def use_conn(
    conn: sqlite3.Connection | None,
    path: str | None,
) -> Generator[sqlite3.Connection, None, None]:
    if conn is not None:
        yield conn
    else:
        with connect(path) as fresh:
            yield fresh


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, declaration in _ADDED_COLUMNS:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in present:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError as exc:
            # Another process starting on the same file may have won the race.
            if "duplicate column" not in str(exc):
                raise


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _add_missing_columns(conn)
    # Older databases may contain guards from the retired browser-wide deletion feature.
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'privacy_*'"
    ).fetchall():
        name = row["name"].replace('"', '""')
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    conn.execute("DROP TABLE IF EXISTS privacy_erased_clients")
    conn.execute("DROP TABLE IF EXISTS privacy_erased_runs")
    conn.execute("PRAGMA journal_mode=WAL")  # Readers do not block writers.


def checkpoint_wal(db_path: str | None = None) -> None:
    """Graceful shutdown flushes WAL transactions into the main database
    file for file-only copies.
    """
    if os.getenv("COSCIENTIST_LITESTREAM_ACTIVE") == "1":
        return
    try:
        with connect(db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        logger.warning("WAL checkpoint failed: %s", exc)


def list_by_run(
    table: str,
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
    *,
    json_fields: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    with use_conn(conn, db_path) as active:
        rows = active.execute(
            f"SELECT * FROM {table} WHERE run_id=? ORDER BY created_at ASC",
            (run_id,),
        ).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        for field in json_fields:
            row[field] = json.loads(row.pop(f"{field}_json", None) or "[]")
    return result
