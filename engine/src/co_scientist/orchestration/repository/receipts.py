from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from co_scientist.platform.db import current_time, use_conn
from co_scientist.platform.db.models import RunRow, row_to_run

if TYPE_CHECKING:
    from co_scientist.core.byok_scope import ByokCredential


@dataclass(frozen=True)
class RunCreationReceipt:
    request_digest: str
    run: RunRow | None


class StagedDocumentsUnavailableError(Exception):
    """A staged document disappeared or changed owner before commit."""


_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._~-]{1,128}")


def valid_run_creation_key(key: str) -> bool:
    return _IDEMPOTENCY_KEY.fullmatch(key) is not None


def lookup_run_creation_receipt(
    client_id: str,
    idempotency_key: str,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> RunCreationReceipt | None:
    """Owner-check the current run even when a stored receipt exists;
    ownership changes must not expose its data.
    """
    with use_conn(conn, db_path) as active:
        receipt = active.execute(
            "SELECT request_digest, run_id FROM run_creation_receipts "
            "WHERE client_id=? AND idempotency_key=?",
            (client_id, idempotency_key),
        ).fetchone()
        if receipt is None:
            return None
        row = active.execute(
            "SELECT * FROM runs WHERE id=? AND client_id=?",
            (receipt["run_id"], client_id),
        ).fetchone()
    return RunCreationReceipt(
        request_digest=str(receipt["request_digest"]),
        run=row_to_run(row) if row is not None else None,
    )


def add_run_creation_receipt(
    client_id: str,
    idempotency_key: str,
    request_digest: str,
    run_id: str,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    with use_conn(conn, db_path) as active:
        active.execute(
            "INSERT INTO run_creation_receipts "
            "(client_id, idempotency_key, request_digest, run_id, created_at) "
            "VALUES (?,?,?,?,?)",
            (client_id, idempotency_key, request_digest, run_id, current_time()),
        )


def _recheck_receipt_and_documents(
    conn: sqlite3.Connection,
    client_id: str,
    idempotency_key: str | None,
    staged_documents: list[dict[str, object]],
) -> tuple[list[dict[str, object]], RunCreationReceipt | None]:
    from co_scientist.domains.documents.repository import get_staged_documents

    if idempotency_key is not None:
        receipt = lookup_run_creation_receipt(client_id, idempotency_key, conn=conn)
        if receipt is not None:
            return [], receipt
    document_ids = [str(document["id"]) for document in staged_documents]
    current = get_staged_documents(document_ids, client_id, conn=conn)
    if len(current) != len(document_ids):
        raise StagedDocumentsUnavailableError
    return current, None


def _index_staged_documents_for_run(
    run_id: str,
    staged: list[dict[str, Any]],
    source: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    from co_scientist.domains.documents.repository import mark_documents_used_by_run
    from co_scientist.domains.research_state.repository.records import NewEvidence, add_evidence

    for document in staged:
        add_evidence(
            NewEvidence(
                run_id=run_id,
                title=str(document["title"]),
                source=source,
                abstract=str(document["text"]),
                mime_type=str(document["mime_type"]),
                sha256=str(document["sha256"]),
                byte_size=int(document["byte_size"]),
                document_version=str(document["sha256"]),
                extraction_tool=str(document["extraction_tool"]),
            ),
            conn=conn,
        )
    mark_documents_used_by_run(run_id, [str(document["id"]) for document in staged], conn=conn)


def commit_run_creation(
    client_id: str,
    idempotency_key: str | None,
    request_digest: str | None,
    staged_documents: list[dict[str, object]],
    byok: ByokCredential | None,
    create_run: Callable[[sqlite3.Connection], RunRow],
    attachment_source: str,
) -> tuple[RunRow | None, RunCreationReceipt | None]:
    """Recheck receipt and staged ownership under the same writer lock as
    setup, credentials and attachment persistence.
    """
    from co_scientist.domains.access import credentials
    from co_scientist.platform.db import transaction

    if (idempotency_key is None) != (request_digest is None):
        raise ValueError("an idempotency key and request digest must be paired")
    with transaction() as conn:
        current_documents, receipt = _recheck_receipt_and_documents(
            conn, client_id, idempotency_key, staged_documents
        )
        if receipt is not None:
            return None, receipt
        run = create_run(conn)
        if byok is not None:
            credentials.store_run_credential(run.id, client_id, byok, conn=conn)
        _index_staged_documents_for_run(run.id, current_documents, attachment_source, conn=conn)
        if idempotency_key is not None and request_digest is not None:
            add_run_creation_receipt(
                client_id,
                idempotency_key,
                request_digest,
                run.id,
                conn=conn,
            )
    return run, None
