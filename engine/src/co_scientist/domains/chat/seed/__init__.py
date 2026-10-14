from __future__ import annotations

import gzip
import json
import logging
import sqlite3
from importlib import resources
from typing import Any

from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import DEMO_CLIENT_ID

logger = logging.getLogger(__name__)

# Tables in foreign-key-safe insert order, as written into the snapshot.
_RUN_SCOPED = (
    "hypotheses",
    "evidence",
    "citations",
    "reviews",
    "matches",
    "claim_evidence",
    "proximity_edges",
    "run_events",
    "run_metrics",
    "reports",
    "messages",
)


def _read_snapshot() -> dict[str, Any]:
    raw = (
        resources.files("co_scientist.domains.chat").joinpath("data/demo_runs.json.gz").read_bytes()
    )
    snapshot: dict[str, Any] = json.loads(gzip.decompress(raw))
    return snapshot


def _matches_seeded_source(
    conn: sqlite3.Connection, snapshot: dict[str, Any], expected_run: dict[str, Any]
) -> bool:
    """Validate the packaged source identity, version, report and transcript."""
    run = conn.execute(
        "SELECT * FROM runs WHERE id=? AND client_id=?",
        (expected_run["id"], DEMO_CLIENT_ID),
    ).fetchone()
    if run is None or run["status"] != "completed":
        return False
    expected_config = json.loads(expected_run["config_json"])
    if (
        expected_config.get("demo_seed_version") != snapshot["version"]
        or expected_config.get("example_chat_version") != snapshot["version"]
    ):
        return False
    if any(run[key] != value for key, value in expected_run.items()):
        return False

    report = conn.execute("SELECT 1 FROM reports WHERE run_id=?", (expected_run["id"],)).fetchone()
    if report is None:
        return False

    interview_id = expected_config.get("interview_id")
    expected_interview = next(
        (row for row in snapshot["tables"]["interviews"] if row["id"] == interview_id),
        None,
    )
    if expected_interview is None:
        return False
    actual_interview = conn.execute(
        "SELECT * FROM interviews WHERE id=? AND client_id=?",
        (interview_id, DEMO_CLIENT_ID),
    ).fetchone()
    if actual_interview is None or any(
        actual_interview[key] != value for key, value in expected_interview.items()
    ):
        return False

    expected_turns = [
        row for row in snapshot["tables"]["interview_turns"] if row["interview_id"] == interview_id
    ]
    actual_turns = conn.execute(
        "SELECT interview_id, role, content, reasoning, fallback, questions_json, created_at "
        "FROM interview_turns WHERE interview_id=? ORDER BY id ASC",
        (interview_id,),
    ).fetchall()
    return len(actual_turns) == len(expected_turns) and all(
        all(actual[key] == value for key, value in expected.items())
        for actual, expected in zip(actual_turns, expected_turns, strict=True)
    )


def is_current_demo_run(
    run_id: str,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Return whether a row is one of the unchanged, packaged public examples."""
    try:
        snapshot = _read_snapshot()
        expected = next((row for row in snapshot["tables"]["runs"] if row["id"] == run_id), None)
        if expected is None:
            return False
        if conn is not None:
            return _matches_seeded_source(conn, snapshot, expected)
        with db.connect(db_path) as active:
            return _matches_seeded_source(active, snapshot, expected)
    except Exception:
        return False


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(row.values()))


def _load_run(
    snapshot: dict[str, Any], run_row: dict[str, Any], stale: Any, db_path: str | None
) -> None:
    tables = snapshot["tables"]
    snapshot_id = run_row["id"]
    run_id = snapshot_id
    interview_id = json.loads(run_row["config_json"])["interview_id"]
    with db.transaction(db_path) as conn:
        if stale:
            old_interview = stale.config.get("interview_id")
            conn.execute("DELETE FROM runs WHERE id=?", (stale.id,))
            if old_interview:
                conn.execute(
                    "DELETE FROM interviews WHERE id=? AND client_id=?",
                    (old_interview, DEMO_CLIENT_ID),
                )
        conn.execute(
            "DELETE FROM interviews WHERE id=? AND client_id=?",
            (interview_id, DEMO_CLIENT_ID),
        )
        _insert(conn, "runs", {**run_row, "id": run_id})
        for table in ("interviews", "interview_turns"):
            for row in tables[table]:
                if row.get("id") == interview_id or row.get("interview_id") == interview_id:
                    _insert(conn, table, row)
        for table in _RUN_SCOPED:
            for row in tables[table]:
                if row["run_id"] == snapshot_id:
                    row = {**row, "run_id": run_id}
                    if table == "reports":
                        row["payload_json"] = row["payload_json"].replace(snapshot_id, run_id)
                    _insert(conn, table, row)
        hypothesis_ids = {h["id"] for h in tables["hypotheses"] if h["run_id"] == snapshot_id}
        for row in tables["hypothesis_state"]:
            if row["hypothesis_id"] in hypothesis_ids:
                _insert(conn, "hypothesis_state", row)


async def seed_demo_runs(db_path: str | None = None) -> None:
    """Never raises: a failed example must not abort startup or later examples."""
    try:
        snapshot = _read_snapshot()
    except Exception:
        logger.exception("Failed to read the demo run snapshot")
        return
    try:
        canonical_ids = {row["id"] for row in snapshot["tables"]["runs"]}
        canonical_interview_ids = {
            json.loads(row["config_json"])["interview_id"] for row in snapshot["tables"]["runs"]
        }
        # The reserved owner is a server-managed namespace. Remove legacy rows
        # that were created under that identity before admission was restricted.
        with db.transaction(db_path) as conn:
            all_demos = conn.execute(
                "SELECT id FROM runs WHERE client_id=?", (DEMO_CLIENT_ID,)
            ).fetchall()
            for row in all_demos:
                if row["id"] not in canonical_ids:
                    conn.execute(
                        "DELETE FROM runs WHERE id=? AND client_id=?",
                        (row["id"], DEMO_CLIENT_ID),
                    )
            placeholders = ",".join("?" for _ in canonical_interview_ids)
            conn.execute(
                f"DELETE FROM interviews WHERE client_id=? AND id NOT IN ({placeholders})",
                (DEMO_CLIENT_ID, *sorted(canonical_interview_ids)),
            )

        existing = {
            run.id: run for run in runs.list_runs(client_id=DEMO_CLIENT_ID, db_path=db_path)
        }
    except Exception:
        logger.exception("Failed to clean legacy demo rows")
        return
    for run_row in snapshot["tables"]["runs"]:
        stale = existing.get(run_row["id"])
        try:
            if stale and is_current_demo_run(stale.id, db_path=db_path):
                logger.info("demo run %s is current, skipping", stale.id[:8])
                continue
            _load_run(snapshot, run_row, stale, db_path)
            logger.info("Loaded demo run %.60s", run_row["research_goal"])
        except Exception:
            logger.exception("Failed to load demo run for goal: %.60s", run_row["research_goal"])
