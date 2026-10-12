from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from co_scientist.platform.db import current_time, list_by_run, use_conn


def replace_knowledge_facts(
    run_id: str,
    facts: list[dict[str, Any]],
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Re-finalization reconstructs the graph wholesale; replacement
    prevents duplicate facts after resume.
    """
    with use_conn(conn, db_path) as conn:
        conn.execute("DELETE FROM knowledge_facts WHERE run_id = ?", (run_id,))
        now = current_time()
        conn.executemany(
            "INSERT INTO knowledge_facts (run_id, hypothesis_id, "
            "evidence_id, kind, statement, entities_json, state, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    fact["hypothesis_id"],
                    fact.get("evidence_id"),
                    fact["kind"],
                    fact["statement"],
                    json.dumps(fact.get("entities") or []),
                    fact["state"],
                    now,
                )
                for fact in facts
            ],
        )


def list_knowledge_facts(
    run_id: str,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    return list_by_run("knowledge_facts", run_id, db_path, conn, json_fields=("entities",))


def save_report(
    run_id: str,
    payload: dict[str, Any],
    markdown: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, str]:
    report_id = str(uuid.uuid4())
    with use_conn(conn, db_path) as active:
        active.execute(
            "INSERT INTO reports "
            "(id, run_id, payload_json, markdown_text, "
            "created_at) VALUES (?,?,?,?,?)",
            (
                report_id,
                run_id,
                json.dumps(payload),
                markdown,
                current_time(),
            ),
        )
    return {"id": report_id}


def get_latest_report(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with use_conn(conn, db_path) as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "payload": json.loads(row["payload_json"]),
            "markdown_path": "",
            "markdown_text": row["markdown_text"],
            "created_at": row["created_at"],
        }


def read_report_markdown(run_id: str, db_path: str | None = None) -> str | None:
    latest = get_latest_report(run_id, db_path=db_path)
    text = latest["markdown_text"] if latest else None
    return text if isinstance(text, str) else None
