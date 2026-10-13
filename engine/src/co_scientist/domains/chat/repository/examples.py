"""An owner gets one private copy of each curated example."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from co_scientist.domains.chat.repository import interviews
from co_scientist.domains.chat.seed import is_current_demo_run
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.launch_control import require_unpaused
from co_scientist.platform.db.models import DEMO_CLIENT_ID
from co_scientist.platform.db.runs import RunCreateOptions
from co_scientist.platform.db.storage_admission import current_peer, reserve_write

# Copy scientific artifacts only. No credentials, tasks or logs.
_TABLES = (
    "hypotheses",
    "hypothesis_state",
    "evidence",
    "citations",
    "reviews",
    "matches",
    "safety_decisions",
    "claim_evidence",
    "knowledge_facts",
    "proximity_edges",
    "run_metrics",
    "reports",
    "run_events",
    "messages",
    "supervisor_plan",
    "supervisor_allocations",
    "retrieval_calls",
)


def _remap(value: Any, identities: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in identities.items():
            value = value.replace(old, new)
    return value


def _insert_copy(
    conn: sqlite3.Connection,
    table: str,
    row: dict[str, Any],
    identities: dict[str, str],
) -> None:
    copied = {
        key: _remap(value, identities)
        for key, value in row.items()
        if key != "id" or not isinstance(value, int)
    }
    conn.execute(
        f"INSERT INTO {table} ({','.join(copied)}) VALUES ({','.join('?' for _ in copied)})",
        tuple(copied.values()),
    )


def _example_source(conn: sqlite3.Connection, source_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
    if not is_current_demo_run(source_id, conn=conn):
        raise ValueError("example not found")
    source_row = conn.execute(
        "SELECT * FROM runs WHERE id=? AND client_id=?",
        (source_id, DEMO_CLIENT_ID),
    ).fetchone()
    if source_row is None:
        raise ValueError("example not found")
    source_config = json.loads(source_row["config_json"])
    if source_row["status"] != "completed" or not source_config.get("example_chat_version"):
        raise ValueError("example chat is not available")
    interview = interviews.get_interview(str(source_config.get("interview_id") or ""), conn=conn)
    if interview is None or interview["client_id"] != DEMO_CLIENT_ID:
        raise ValueError("example chat is not available")
    return source_row, interview


def open_example_chat(source_id: str, owner: str) -> dict[str, Any]:
    if not owner or owner == DEMO_CLIENT_ID:
        raise ValueError("an owned example chat is required")
    # Reuse checks and the entire copy commit together. A double click or two
    # tabs cannot duplicate chats. No network occurs inside this lock.
    with db.transaction() as conn:
        require_unpaused(conn=conn)
        conn.execute("PRAGMA defer_foreign_keys=ON")
        source_row, interview = _example_source(conn, source_id)
        existing = conn.execute(
            "SELECT id,config_json FROM runs WHERE client_id=? AND "
            "json_extract(config_json, '$.example_source_id')=? AND "
            "json_extract(config_json, '$.example_chat_version')=?",
            (owner, source_id, json.loads(source_row["config_json"])["example_chat_version"]),
        ).fetchone()
        if existing:
            existing_config = json.loads(existing["config_json"])
            result = interviews.get_interview(existing_config["interview_id"], conn=conn)
            if result:
                opened_at = db.current_time()
                conn.execute(
                    "UPDATE interviews SET updated_at=? WHERE id=?",
                    (opened_at, result["id"]),
                )
                conn.execute(
                    "UPDATE runs SET updated_at=? WHERE id=?",
                    (opened_at, existing["id"]),
                )
                return {**result, "updated_at": opened_at}
        interview_id = str(uuid.uuid4())
        config = {
            **json.loads(source_row["config_json"]),
            "interview_id": interview_id,
            "example_source_id": source_id,
        }
        target = runs.create_run(
            source_row["research_goal"],
            source_row["profile"],
            source_row["provider"],
            config,
            RunCreateOptions(
                client_id=owner,
                title=source_row["title"],
                llm_backend="offline",
                conn=conn,
            ),
        )
        identities = {source_id: target.id, interview["id"]: interview_id}
        rows: dict[str, list[dict[str, Any]]] = {}
        for table in _TABLES:
            predicate = (
                "hypothesis_id IN (SELECT id FROM hypotheses WHERE run_id=?)"
                if table == "hypothesis_state"
                else "run_id=?"
            )
            rows[table] = [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {table} WHERE {predicate}", (source_id,))
            ]
            for row in rows[table]:
                if isinstance(row.get("id"), str):
                    identities[row["id"]] = str(uuid.uuid4())
        interview_row = dict(
            conn.execute("SELECT * FROM interviews WHERE id=?", (interview["id"],)).fetchone()
        )
        interview_row.update(
            client_id=owner,
            created_at=target.created_at,
            updated_at=target.updated_at,
            completed_at=target.updated_at,
        )
        turns = [
            dict(turn)
            for turn in conn.execute(
                "SELECT * FROM interview_turns WHERE interview_id=? ORDER BY id",
                (interview["id"],),
            )
        ]
        copied_rows = [
            dict(source_row),
            interview_row,
            *turns,
            *(row for table_rows in rows.values() for row in table_rows),
        ]
        copied_bytes = sum(
            len(json.dumps(row, ensure_ascii=False).encode()) + 256 for row in copied_rows
        )
        # Tiny copy requests must pay for their persisted amplification, under the same writer.
        reserve_write(owner, current_peer(), copied_bytes, conn=conn, requests=0)
        _insert_copy(conn, "interviews", interview_row, identities)
        for turn in turns:
            _insert_copy(conn, "interview_turns", turn, identities)
        for table, table_rows in rows.items():
            for row in table_rows:
                _insert_copy(conn, table, row, identities)
        conn.execute(
            "UPDATE runs SET status='completed',created_at=?,completed_at=?,"
            "goal_restatement=? WHERE id=?",
            (
                source_row["created_at"],
                source_row["completed_at"],
                source_row["goal_restatement"],
                target.id,
            ),
        )
        result = interviews.get_interview(interview_id, conn=conn)
        assert result is not None
        return result
