from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from co_scientist.platform.db import connect, current_time, transaction, use_conn
from co_scientist.platform.db.launch_control import require_unpaused
from co_scientist.platform.db.models import MessageRow, row_to_message
from co_scientist.platform.db.storage_admission import check_run_input_storage, current_peer


@dataclass(frozen=True)
class NewMessage:
    run_id: str
    sender: str
    content: str
    kind: str
    meta: dict[str, Any] | None = None


def append_message(
    message: NewMessage,
    db_path: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> MessageRow:
    now = current_time()
    meta_json = json.dumps(message.meta) if message.meta is not None else None
    with nullcontext(conn) if conn is not None else transaction(db_path) as conn:
        if message.sender == "user" or message.kind == "steering":
            require_unpaused(conn=conn)
        if message.sender == "user" or message.kind == "steering":
            check_run_input_storage(
                conn,
                message.run_id,
                "messages",
                len(message.content.encode()) + len((meta_json or "").encode()),
            )
        cursor = conn.execute(
            "INSERT INTO messages (run_id, sender, content, kind, "
            "created_at, applied, meta_json, peer_hash) VALUES (?,?,?,?,?,0,?,?)",
            (
                message.run_id,
                message.sender,
                message.content,
                message.kind,
                now,
                meta_json,
                current_peer(),
            ),
        )
        msg_id = cursor.lastrowid or 0
    return MessageRow(
        id=msg_id,
        run_id=message.run_id,
        sender=message.sender,
        content=message.content,
        kind=message.kind,
        created_at=now,
        applied=False,
        meta=message.meta,
    )


_MESSAGE_COLUMNS = (
    "id, run_id, sender, content, kind, created_at, applied, meta_json, "
    "applied_at, applied_decision"
)


def list_messages(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[MessageRow]:
    with use_conn(conn, db_path) as conn:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE run_id=? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
        return [row_to_message(r) for r in rows]


def claim_start_prompt(
    run_id: str, prompt: str, *, conn: sqlite3.Connection | None = None
) -> tuple[MessageRow, bool]:
    with nullcontext(conn) if conn is not None else transaction() as conn:
        require_unpaused(conn=conn)
        claim = conn.execute(
            "SELECT prompt_message_id FROM run_announcements WHERE run_id=?",
            (run_id,),
        ).fetchone()
        row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE run_id=? "
            "AND kind='start' AND sender='user' ORDER BY id LIMIT 1",
            (run_id,),
        ).fetchone()
        fresh = claim is None and row is None
        if fresh:
            check_run_input_storage(conn, run_id, "messages", len(prompt.encode()))
            cursor = conn.execute(
                "INSERT INTO messages (run_id,sender,content,kind,created_at,applied,peer_hash) "
                "VALUES (?,'user',?,'start',?,0,?)",
                (run_id, prompt, current_time(), current_peer()),
            )
            row = conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()
        if row is None:
            raise ValueError("announcement prompt is missing")
        message = row_to_message(row)
        conn.execute(
            "INSERT OR IGNORE INTO run_announcements VALUES (?,?)",
            (run_id, message.id),
        )
        return message, fresh


def append_qa_reply(message: NewMessage, question_id: int) -> None:
    """A replaced question invalidates answers still streaming in other tabs."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages "
            "(run_id,sender,content,kind,created_at,applied,meta_json) "
            "SELECT ?,?,?, 'qa',?,0,? WHERE EXISTS "
            "(SELECT 1 FROM messages WHERE run_id=? AND id=? "
            "AND kind='qa' AND sender='user')",
            (
                message.run_id,
                message.sender,
                message.content,
                current_time(),
                json.dumps(message.meta) if message.meta is not None else None,
                message.run_id,
                question_id,
            ),
        )


def rewind_qa(
    run_id: str, message_id: int, question: str | None, *, conn: sqlite3.Connection | None = None
) -> MessageRow:
    """Rewind only Q&A; run inputs and scientific state are immutable here."""
    with nullcontext(conn) if conn is not None else transaction() as conn:
        require_unpaused(conn=conn)
        target = conn.execute(
            "SELECT sender, content, kind FROM messages WHERE run_id=? AND id=?",
            (run_id, message_id),
        ).fetchone()
        if target is None or target["kind"] != "qa":
            raise ValueError("Only Q&A turns can be revised")
        if question is not None:
            if target["sender"] != "user":
                raise ValueError("Only user questions can be edited")
            question_id = message_id
            text = question
        else:
            if target["sender"] != "system":
                raise ValueError("Only answers can be retried")
            previous = conn.execute(
                "SELECT id, content FROM messages WHERE run_id=? AND kind='qa' "
                "AND sender='user' AND id<? ORDER BY id DESC LIMIT 1",
                (run_id, message_id),
            ).fetchone()
            if previous is None:
                raise ValueError("Answer has no preceding question")
            question_id, text = previous["id"], previous["content"]
        conn.execute(
            "DELETE FROM messages WHERE run_id=? AND kind='qa' AND id>=?",
            (run_id, question_id),
        )
        check_run_input_storage(conn, run_id, "messages", len(text.encode()))
        cursor = conn.execute(
            "INSERT INTO messages "
            "(run_id,sender,content,kind,created_at,applied,peer_hash) "
            "VALUES (?,'user',?,'qa',?,0,?)",
            (run_id, text, current_time(), current_peer()),
        )
        row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
        return row_to_message(row)


def get_pending_steering(run_id: str, db_path: str | None = None) -> list[MessageRow]:
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE run_id=? AND kind='steering' AND applied=0 ORDER BY id ASC",
            (run_id,),
        ).fetchall()
        return [row_to_message(r) for r in rows]


def mark_steering_applied(
    ids: list[int],
    conn: sqlite3.Connection | None = None,
    *,
    decision: str | None = None,
) -> None:
    """Acknowledge guidance in the checkpoint transaction; a separate commit
    could mark input applied before its state survives.
    """
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with use_conn(conn, None) as conn:
        conn.execute(
            "UPDATE messages SET applied=1, applied_at=?, applied_decision=? "
            f"WHERE id IN ({placeholders})",
            (current_time(), decision, *ids),
        )
