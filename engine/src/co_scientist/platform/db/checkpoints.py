from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from co_scientist.platform.db import checkpoint_wal, connect, current_time, use_conn
from co_scientist.platform.db.supervisor_plan import (
    sync_supervisor_ledger_from_checkpoint,
)

# Bump incompatible envelope changes; mismatched versions fail closed.
CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class NewCheckpoint:
    stage: str
    schema_version: int
    last_event_seq: int
    state: dict[str, Any]


def _insert_checkpoint_row(conn: sqlite3.Connection, run_id: str, checkpoint: NewCheckpoint) -> int:
    # Allocate the sequence and insert in one SQL statement; concurrent
    # checkpoint writers must not choose the same sequence.
    row = conn.execute(
        "INSERT INTO checkpoints (run_id, seq, stage, schema_version, "
        "last_event_seq, state_json, created_at) VALUES (?, "
        "(SELECT COALESCE(MAX(seq), 0) + 1 FROM checkpoints "
        "WHERE run_id=?), ?, ?, ?, ?, ?) RETURNING seq",
        (
            run_id,
            run_id,
            checkpoint.stage,
            checkpoint.schema_version,
            checkpoint.last_event_seq,
            json.dumps(checkpoint.state),
            current_time(),
        ),
    ).fetchone()
    return int(row["seq"])


def _prune_older_checkpoints(conn: sqlite3.Connection, run_id: str, seq: int) -> None:
    """Only the newest checkpoint is resumable; retaining its sequence
    preserves monotonic ordering while bounding storage per run.
    """
    conn.execute("DELETE FROM checkpoints WHERE run_id=? AND seq<?", (run_id, seq))


def save_checkpoint(
    run_id: str,
    checkpoint: NewCheckpoint,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    with use_conn(conn, db_path) as conn:
        seq = _insert_checkpoint_row(conn, run_id, checkpoint)
        _prune_older_checkpoints(conn, run_id, seq)
        # Checkpoint commits preserve scheduling audit even when failure,
        # cancellation or safety prevents finalization.
        sync_supervisor_ledger_from_checkpoint(run_id, checkpoint.state, conn)
    return seq


def get_latest_checkpoint(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with use_conn(conn, db_path) as conn:
        row = conn.execute(
            "SELECT seq, stage, schema_version, last_event_seq, state_json "
            "FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "seq": row["seq"],
        "stage": row["stage"],
        "schema_version": row["schema_version"],
        "last_event_seq": row["last_event_seq"],
        "state": json.loads(row["state_json"]),
    }


def latest_checkpoint_seq(run_id: str, conn: sqlite3.Connection) -> int:
    """Commit guards only compare sequences; parsing the full state under
    the write lock costs milliseconds per megabyte.
    """
    row = conn.execute(
        "SELECT seq FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return int(row["seq"]) if row else 0


def has_checkpoint(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    with use_conn(conn, db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM checkpoints WHERE run_id=? LIMIT 1",
            (run_id,),
        ).fetchone()
    return row is not None


# On a full volume, prune in small committed batches before new writes; a large
# delete can exhaust WAL/rollback headroom.
_PRUNE_BATCH_ROWS = 4


def prune_superseded_checkpoints(db_path: str | None = None) -> int:
    """Small commits reclaim legacy history even when a full volume lacks
    room for one large delete and its rollback journal.
    """
    superseded = (
        "SELECT stale.rowid FROM checkpoints AS stale WHERE stale.seq < ("
        "SELECT MAX(newer.seq) FROM checkpoints AS newer "
        "WHERE newer.run_id = stale.run_id) LIMIT ?"
    )
    delete = f"DELETE FROM checkpoints WHERE rowid IN ({superseded})"

    checkpoint_wal(db_path)

    total = 0
    while True:
        with connect(db_path) as owned:
            deleted = int((owned.execute(delete, (_PRUNE_BATCH_ROWS,)).rowcount) or 0)
        if deleted == 0:
            return total
        total += deleted
        checkpoint_wal(db_path)


def clear_checkpoints(run_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Rebootstrap expects sequence zero; true engine resume instead retains
    its latest checkpoint.
    """
    with use_conn(conn, None) as conn:
        conn.execute("DELETE FROM checkpoints WHERE run_id=?", (run_id,))


def live_pool_sizes(run_id: str, conn: sqlite3.Connection) -> tuple[int, int] | None:
    """Run detail polls need two lengths of the latest state; SQLite counts
    them without building the whole state in Python.
    """
    row = conn.execute(
        "SELECT json_array_length(state_json, '$.state.hypotheses') AS hypotheses, "
        "json_array_length(state_json, '$.state.articles') AS articles "
        "FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row["hypotheses"] or 0), int(row["articles"] or 0)
