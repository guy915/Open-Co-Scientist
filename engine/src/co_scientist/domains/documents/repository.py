from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from co_scientist.platform.db import connect, current_time, transaction, use_conn
from co_scientist.platform.db.storage_admission import check_staged_storage, current_peer

# Interview excerpts need enough scope for planning; full-text corpus retrieval
# belongs to the research run.
INTERVIEW_EXCERPT_CHARS = 4_000


@dataclass(frozen=True)
class NewStagedDocument:
    client_id: str
    title: str
    text: str
    mime_type: str
    sha256: str
    byte_size: int
    extraction_tool: str


def add_staged_document(document: NewStagedDocument) -> str:
    document_id = str(uuid.uuid4())
    with transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM staged_documents WHERE client_id=? AND sha256=? "
            "AND text=? AND mime_type=?",
            (document.client_id, document.sha256, document.text, document.mime_type),
        ).fetchone()
        if existing:
            return str(existing[0])
        peer = current_peer()
        check_staged_storage(
            conn,
            document.client_id,
            peer,
            len(document.text.encode()) + len(document.title.encode()),
        )
        conn.execute(
            "INSERT INTO staged_documents (id, client_id, title, text, "
            "mime_type, sha256, byte_size, extraction_tool, created_at, peer_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                document_id,
                document.client_id,
                document.title,
                document.text,
                document.mime_type,
                document.sha256,
                document.byte_size,
                document.extraction_tool,
                current_time(),
                peer,
            ),
        )
    return document_id


def _rows_to_documents(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: value for key, value in dict(row).items() if key != "peer_hash"} for row in rows]


def get_staged_documents(
    document_ids: list[str],
    client_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Unknown and other-owner IDs are indistinguishable to avoid leaking
    document existence.
    """
    if not document_ids:
        return []
    placeholders = ",".join("?" for _ in document_ids)
    with use_conn(conn, None) as active:
        rows = active.execute(
            f"SELECT * FROM staged_documents WHERE id IN ({placeholders}) AND client_id=?",
            (*document_ids, client_id),
        ).fetchall()
    by_id = {str(row["id"]): dict(row) for row in rows}
    return [by_id[doc_id] for doc_id in document_ids if doc_id in by_id]


def attach_documents_to_interview(
    interview_id: str,
    document_ids: list[str],
    client_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    if not document_ids:
        return 0
    placeholders = ",".join("?" for _ in document_ids)
    with use_conn(conn, None) as conn:
        return int(
            conn.execute(
                "UPDATE staged_documents SET interview_id=? "
                f"WHERE id IN ({placeholders}) AND client_id=?",
                (interview_id, *document_ids, client_id),
            ).rowcount
        )


def list_interview_documents(interview_id: str) -> list[dict[str, Any]]:
    with connect() as active:
        rows = active.execute(
            "SELECT * FROM staged_documents WHERE interview_id=? ORDER BY created_at ASC",
            (interview_id,),
        ).fetchall()
    return _rows_to_documents(rows)


def merge_run_setup_documents(
    named: list[dict[str, Any]],
    interview_id: str | None,
) -> list[dict[str, Any]]:
    from_chat = list_interview_documents(interview_id) if interview_id is not None else []
    resolved: dict[str, dict[str, Any]] = {}
    for document in [*named, *from_chat]:
        resolved.setdefault(str(document["id"]), document)
    return list(resolved.values())


def interview_document_excerpts(interview_id: str) -> list[dict[str, str]]:
    """Mark clipped excerpts so the model never mistakes a truncated
    attachment for its complete text.
    """
    documents = list_interview_documents(interview_id)
    excerpts = []
    for document in documents:
        text = str(document["text"])
        clipped = text[:INTERVIEW_EXCERPT_CHARS]
        if len(text) > INTERVIEW_EXCERPT_CHARS:
            clipped += "\n[document continues; excerpt truncated]"
        excerpts.append({"title": str(document["title"]), "excerpt": clipped})
    return excerpts


def delete_staged_document(document_id: str, client_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM staged_documents WHERE id=? AND client_id=?",
            (document_id, client_id),
        )
    return cur.rowcount > 0


def delete_staged_documents_older_than(cutoff: float) -> int:
    """Age is measured from creation; staged rows have no last-use
    timestamp.
    """
    with connect() as conn:
        cur = conn.execute("DELETE FROM staged_documents WHERE created_at < ?", (cutoff,))
    return cur.rowcount


def mark_documents_used_by_run(
    run_id: str,
    document_ids: list[str],
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    if not document_ids:
        return
    placeholders = ",".join("?" for _ in document_ids)
    with use_conn(conn, None) as active:
        active.execute(
            f"UPDATE staged_documents SET run_id=? WHERE id IN ({placeholders})",
            (run_id, *document_ids),
        )
