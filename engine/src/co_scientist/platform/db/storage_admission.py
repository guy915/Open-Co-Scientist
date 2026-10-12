from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Literal

from co_scientist.core.exceptions import StorageAdmissionError
from co_scientist.platform.db import current_time, transaction

_peer: ContextVar[str] = ContextVar("storage_connecting_peer", default="unknown")


def current_peer() -> str:
    return _peer.get()


@contextmanager
def scoped_peer(peer: str) -> Iterator[None]:
    token = _peer.set(peer)
    try:
        yield
    finally:
        _peer.reset(token)


def reserve_write(
    owner: str, peer: str, size: int, *, conn: sqlite3.Connection | None = None, requests: int = 1
) -> None:
    from co_scientist.core.config import settings

    day = int(current_time() // 86400)
    budgets = (
        (
            "global",
            "",
            settings.anonymous_write_global_requests_per_day,
            settings.anonymous_write_global_bytes_per_day,
        ),
        (
            "client",
            owner,
            settings.anonymous_write_client_requests_per_day,
            settings.anonymous_write_client_bytes_per_day,
        ),
        (
            "host",
            peer,
            settings.anonymous_write_host_requests_per_day,
            settings.anonymous_write_host_bytes_per_day,
        ),
    )
    # Denied handlers, failed parses and deletions never refund daily admission.
    with nullcontext(conn) if conn is not None else transaction() as conn:
        pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        if (pages - free) * page_size + max(size, 512) * 4 > settings.anonymous_store_max_bytes:
            raise StorageAdmissionError("service storage admission exhausted")
        for scope, subject, row_limit, byte_limit in budgets:
            row = conn.execute(
                "SELECT requests,bytes FROM input_admissions WHERE day=? AND scope=? AND subject=?",
                (day, scope, subject),
            ).fetchone()
            count, used = (int(row[0]), int(row[1])) if row else (0, 0)
            if count + requests > row_limit or used + max(size, 512) > byte_limit:
                raise StorageAdmissionError("daily input admission exhausted")
        conn.execute("DELETE FROM input_admissions WHERE day<?", (day,))
        for scope, subject, _, _ in budgets:
            conn.execute(
                "INSERT INTO input_admissions VALUES (?,?,?,?,?) "
                "ON CONFLICT(day,scope,subject) DO UPDATE SET "
                "requests=requests+excluded.requests,bytes=bytes+excluded.bytes",
                (day, scope, subject, requests, max(size, 512)),
            )


def check_staged_storage(conn: sqlite3.Connection, owner: str, peer: str, size: int) -> None:
    from co_scientist.core.config import settings

    for condition, args, rows, byte_limit in (
        ("1", (), 1000, settings.staged_document_global_bytes),
        ("client_id=?", (owner,), 32, settings.staged_document_client_bytes),
        ("peer_hash IN (?, 'unknown')", (peer,), 128, settings.staged_document_host_bytes),
    ):
        count, used = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(text AS BLOB))"
            "+LENGTH(CAST(title AS BLOB))+256),0) "
            f"FROM staged_documents WHERE {condition}",
            args,
        ).fetchone()
        if count >= rows or used + size + 256 > byte_limit:
            raise StorageAdmissionError("staged document storage admission exhausted")


def check_run_input_storage(
    conn: sqlite3.Connection, run_id: str, kind: Literal["messages", "corpus"], size: int
) -> None:
    from co_scientist.core.config import settings

    row = conn.execute("SELECT client_id FROM runs WHERE id=?", (run_id,)).fetchone()
    owner = str(row[0]) if row else ""
    peer = current_peer()
    if kind == "messages":
        prefix = "user_message"
        base = "messages i JOIN runs r ON r.id=i.run_id"
        predicate = "(i.sender='user' OR i.kind='steering')"
        expression = (
            "LENGTH(CAST(i.content AS BLOB))+LENGTH(CAST(COALESCE(i.meta_json,'') AS BLOB))+256"
        )
        row_limits = (1000, 2000, 4000, 20000)
    else:
        prefix = "human_corpus"
        base = "evidence i JOIN runs r ON r.id=i.run_id"
        predicate = "i.source='attachment'"
        expression = (
            "LENGTH(CAST(i.abstract AS BLOB))+LENGTH(CAST(i.passage_text AS BLOB))"
            "+LENGTH(CAST(i.title AS BLOB))+256"
        )
        row_limits = (32, 64, 128, 1000)
    for (condition, args, scope), rows in zip(
        (
            ("i.run_id=?", (run_id,), "run"),
            ("r.client_id=?", (owner,), "client"),
            # Legacy records share the fallback bucket and count against each peer.
            ("i.peer_hash IN (?, 'unknown')", (peer,), "host"),
            ("1", (), "global"),
        ),
        row_limits,
        strict=True,
    ):
        count, used = conn.execute(
            f"SELECT COUNT(*),COALESCE(SUM({expression}),0) FROM {base} "
            f"WHERE {predicate} AND {condition}",
            args,
        ).fetchone()
        if count >= rows or used + size + 256 > getattr(settings, f"{prefix}_{scope}_bytes"):
            raise StorageAdmissionError("stored input admission exhausted")
