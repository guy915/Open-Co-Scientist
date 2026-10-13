"""Stage before the interview so attachments inform planning; run creation
then commits copied corpus text atomically.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)

from co_scientist.api.auth import client_id, require_client_scope
from co_scientist.api.documents_access import (
    resolve_owned_documents as resolve_owned_documents,
)
from co_scientist.api.uploads import extract_upload
from co_scientist.core.async_bridge import off_loop
from co_scientist.domains.documents import repository as store
from co_scientist.domains.documents.repository import NewStagedDocument
from co_scientist.domains.documents.staged import document_summary as document_summary

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.post("")
async def stage_document(
    request: Request,
    file: Annotated[UploadFile, File()],
    consent: Annotated[bool, Form()],
) -> dict[str, Any]:
    """Extract one scientist upload and stage it against the caller.

    Args:
        request: Incoming request, used to read the owning client identity.
        file: The uploaded document.
        consent: Explicit consent to index the document's contents.

    Returns:
        The staged document's id and extraction provenance.

    Raises:
        HTTPException: 400 when the caller carries no identity at all (see
            ``co_scientist.api.auth.require_client_scope``); 422 when consent is
            withheld or the document cannot be extracted.
    """
    owner = require_client_scope(request)
    if not consent:
        raise HTTPException(status_code=422, detail="consent is required to index a document")
    extracted = await extract_upload(file, owner=owner)
    title = (file.filename or "Uploaded document").strip()
    document_id = store.add_staged_document(
        NewStagedDocument(
            client_id=owner,
            title=title,
            text=extracted.text,
            mime_type=extracted.mime_type,
            sha256=extracted.sha256,
            byte_size=extracted.byte_size,
            extraction_tool=extracted.extraction_tool,
        )
    )
    return {
        "id": document_id,
        "title": title,
        "sha256": extracted.sha256,
        "byte_size": extracted.byte_size,
        "mime_type": extracted.mime_type,
        "extraction_tool": extracted.extraction_tool,
    }


@router.delete("/{document_id}", status_code=204)
@off_loop
def delete_document(document_id: str, request: Request) -> Response:
    """Permanently delete one document the caller staged (N3).

    Owner-scoped like every other read of ``staged_documents``: a document
    staged by another client is reported as not found rather than
    disclosing that the id exists. Deletion does not cascade to any run
    the document was carried into -- the run keeps the corpus text it
    already indexed, since a run's evidence is its own row (see
    ``mark_documents_used_by_run``), and only the staging record itself is
    removed.

    Raises:
        HTTPException: 404 if the document is unknown, owned by another
            client, or the caller carries no identity at all -- an
            identity-less caller owns nothing, so it can never delete a
            document even one with the same empty subject (legacy data
            predating ``stage_document``'s own-identity requirement).
    """
    owner = client_id(request)
    deleted = owner and store.delete_staged_document(document_id, owner)
    if not deleted:
        raise HTTPException(status_code=404, detail="document not found")
    return Response(status_code=204)
