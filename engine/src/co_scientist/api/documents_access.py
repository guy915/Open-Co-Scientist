from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from co_scientist.domains.documents import repository as store
from co_scientist.platform.db import Connection


def resolve_owned_documents(
    document_ids: list[str], owner: str, *, conn: Connection | None = None
) -> list[dict[str, Any]]:
    """Refuse the entire set before writes: silently dropping a named
    attachment would produce ungrounded work and half-attached runs.
    """
    if not document_ids:
        return []
    documents = store.get_staged_documents(document_ids, owner, conn=conn)
    if len(documents) != len(document_ids):
        raise HTTPException(status_code=404, detail="attached document not found")
    return documents
