from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from co_scientist.platform.db import connect, current_time, use_conn
from co_scientist.platform.db.models import DEMO_CLIENT_ID


def create_interview(
    client_id: str,
    challenge: str,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if client_id == DEMO_CLIENT_ID:
        raise ValueError("the demo identity is reserved for seeded examples")
    interview_id = str(uuid.uuid4())
    now = current_time()
    fields: dict[str, Any] = {
        "research_challenge": challenge.strip(),
        "focus_area": [],
        "preferences": [],
        # Empty lab constraints mean none declared and must not change the
        # unconstrained prompts.
        "lab_constraints": [],
        "title": None,
    }
    with use_conn(conn, db_path) as active:
        active.execute(
            "INSERT INTO interviews (id, client_id, "
            "status, fields_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                interview_id,
                client_id,
                "active",
                json.dumps(fields),
                now,
                now,
            ),
        )
        active.execute(
            "INSERT INTO interview_turns (interview_id, role, content, "
            "created_at) VALUES (?,?,?,?)",
            (interview_id, "user", challenge.strip(), now),
        )
        result = get_interview(interview_id, conn=active)
    assert result is not None
    from co_scientist.platform.telemetry.diagnostic_events import log_chat_turn

    if conn is None:
        log_chat_turn("user", challenge.strip(), owner=client_id)
    return result


def get_interview(
    interview_id: str,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with use_conn(conn, db_path) as active:
        row = active.execute("SELECT * FROM interviews WHERE id=?", (interview_id,)).fetchone()
        if row is None:
            return None
        turns = active.execute(
            "SELECT id, role, content, reasoning, fallback, questions_json, "
            "created_at FROM interview_turns WHERE interview_id=? "
            "ORDER BY id ASC",
            (interview_id,),
        ).fetchall()
    result = dict(row)
    result["fields"] = json.loads(result.pop("fields_json"))
    result["turns"] = [_decoded_turn(turn) for turn in turns]
    return result


def _decoded_turn(turn: sqlite3.Row) -> dict[str, Any]:
    """GET and streamed closing frames share normalization so the same turn
    cannot have conflicting wire representations.
    """
    row = dict(turn)
    raw = row.pop("questions_json", None)
    return {
        **row,
        "fallback": bool(turn["fallback"]),
        "questions": json.loads(raw) if raw else [],
    }


def _interview_run_ids(conn: sqlite3.Connection, client_id: str) -> dict[str, str]:
    """Resolve config links in Python to avoid a JSON1 build dependency;
    each client has few runs.
    """
    rows = conn.execute(
        "SELECT id, config_json FROM runs WHERE client_id=? ORDER BY created_at ASC",
        (client_id,),
    ).fetchall()
    links: dict[str, str] = {}
    for row in rows:
        try:
            config = json.loads(row["config_json"])
        except (TypeError, ValueError):
            continue
        interview_id = config.get("interview_id") if config else None
        if interview_id:
            links[str(interview_id)] = str(row["id"])
    return links


def run_id_for_interview(interview_id: str, client_id: str) -> str | None:
    """Only chat reopening needs this scan; per-turn readers must not pay
    its cost.
    """
    if not client_id:
        return None
    with connect() as conn:
        return _interview_run_ids(conn, client_id).get(str(interview_id))


def _chat_summary(row: sqlite3.Row, run_id: str | None) -> dict[str, Any]:
    fields = json.loads(row["fields_json"])
    return {
        "id": row["id"],
        "title": str(fields.get("title") or "").strip() or None,
        "challenge": str(fields.get("research_challenge") or ""),
        "status": row["status"],
        "run_id": run_id,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_interviews(client_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, status, fields_json, created_at, updated_at "
            "FROM interviews WHERE client_id=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (client_id, limit),
        ).fetchall()
        links = _interview_run_ids(conn, client_id)
    return [_chat_summary(row, links.get(str(row["id"]))) for row in rows]


@dataclass(frozen=True)
class NewInterviewTurn:
    """Persist reasoning, fallback provenance and offered questions so
    reopened chats retain the same context and pending answer.
    """

    role: str
    content: str
    reasoning: str | None = None
    fallback: bool = False
    questions: list[dict[str, Any]] = field(default_factory=list)


def append_interview_turn(
    interview_id: str,
    turn: NewInterviewTurn,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    now = current_time()
    with use_conn(conn, db_path) as active:
        active.execute(
            "INSERT INTO interview_turns (interview_id, role, content, "
            "reasoning, fallback, questions_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                interview_id,
                turn.role,
                turn.content.strip(),
                turn.reasoning or None,
                int(turn.fallback),
                json.dumps(turn.questions) if turn.questions else None,
                now,
            ),
        )
        active.execute("UPDATE interviews SET updated_at=? WHERE id=?", (now, interview_id))
        if turn.role == "user" and conn is None:
            from co_scientist.platform.telemetry.diagnostic_events import log_chat_turn

            owner = active.execute(
                "SELECT client_id FROM interviews WHERE id=?", (interview_id,)
            ).fetchone()
            log_chat_turn(
                "user",
                turn.content,
                owner=owner["client_id"] if owner else None,
            )


def rewind_interview(
    interview_id: str, turn_id: int, *, conn: sqlite3.Connection | None = None
) -> int:
    """Edits and retries invalidate downstream derivations; callers
    reconstruct interview fields from the retained transcript.
    """
    now = current_time()
    with use_conn(conn, None) as conn:
        removed = conn.execute(
            "DELETE FROM interview_turns WHERE interview_id=? AND id>=?",
            (interview_id, turn_id),
        ).rowcount
        if removed:
            conn.execute(
                "UPDATE interviews SET updated_at=? WHERE id=?",
                (now, interview_id),
            )
    return int(removed)


def update_interview(
    interview_id: str,
    fields: Mapping[str, Any],
    current_question: str | None,
    *,
    completed: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    now = current_time()
    status = "completed" if completed else "active"
    with use_conn(conn, None) as conn:
        conn.execute(
            "UPDATE interviews SET fields_json=?, current_question=?, "
            "status=?, updated_at=?, completed_at=? WHERE id=?",
            (
                json.dumps(dict(fields)),
                current_question,
                status,
                now,
                now if completed else None,
                interview_id,
            ),
        )


def delete_interview(interview_id: str) -> dict[str, int]:
    """Transcript rows cascade; staged documents lack an interview FK and
    must be detached without deleting the scientist's copy.
    """
    with connect() as conn:
        turns = conn.execute(
            "SELECT COUNT(*) FROM interview_turns WHERE interview_id=?",
            (interview_id,),
        ).fetchone()[0]
        detached = conn.execute(
            "UPDATE staged_documents SET interview_id=NULL WHERE interview_id=?",
            (interview_id,),
        ).rowcount
        removed = conn.execute("DELETE FROM interviews WHERE id=?", (interview_id,)).rowcount
    return {
        "interviews": int(removed),
        "interview_turns": int(turns),
        "staged_documents_detached": int(detached),
    }
