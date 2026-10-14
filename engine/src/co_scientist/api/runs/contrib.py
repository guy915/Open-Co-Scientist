from __future__ import annotations

from typing import Annotated, Any

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)

import co_scientist.domains.documents.ingest as document_ingest
import co_scientist.platform.retrieval.run_corpus as run_corpus
from co_scientist.api.auth import client_id
from co_scientist.api.runs.models import (
    HumanAttachmentRequest,
)
from co_scientist.api.runs.support import _require_run, prepare_continuation, request_continuation
from co_scientist.api.uploads import extract_upload
from co_scientist.core.async_bridge import off_loop
from co_scientist.domains.chat.repository import messages
from co_scientist.domains.chat.repository.messages import NewMessage
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.records import NewEvidence
from co_scientist.orchestration.repository import events as store
from co_scientist.platform.db import transaction
from co_scientist.platform.db.launch_control import require_unpaused
from co_scientist.platform.db.models import ScientificTask

attachments_router = APIRouter()


def _persist_attachment(
    evidence: NewEvidence, sender: str, guidance: str
) -> tuple[str, ScientificTask | None]:
    admission = prepare_continuation(evidence.run_id)
    # Pause and input storage checks share the writer lock with all three writes.
    with transaction() as conn:
        require_unpaused(conn=conn)
        evidence_id = records.add_evidence(evidence, conn=conn)
        message = messages.append_message(
            NewMessage(
                run_id=evidence.run_id,
                sender=sender,
                content=guidance,
                kind="steering",
                meta={"kind": "attachment", "evidence_id": evidence_id},
            ),
            conn=conn,
        )
        store.append_event_deferred_log(
            evidence.run_id,
            "scientist.attachment",
            {"evidence_id": evidence_id, "title": evidence.title},
            conn,
        )
        continuation = request_continuation(
            evidence.run_id, message.id, conn=conn, admission=admission
        )
    return evidence_id, continuation


def _persist_and_notify_attachment(
    run_id: str, req: HumanAttachmentRequest
) -> tuple[str, ScientificTask | None]:
    return _persist_attachment(
        NewEvidence(
            run_id=run_id,
            title=req.title,
            source=run_corpus.ATTACHMENT_SOURCE,
            abstract=req.text,
        ),
        "scientist",
        f"Use the private research document '{req.title}' in subsequent work.",
    )


@attachments_router.post("/{run_id}/attachments")
@off_loop
def add_attachment(run_id: str, req: HumanAttachmentRequest) -> dict[str, Any]:
    """Attach a consented text document to the run's private corpus."""
    _require_run(run_id)
    if not req.consent:
        raise HTTPException(status_code=422, detail="consent is required to index a document")
    ev_id, continuation = _persist_and_notify_attachment(run_id, req)
    return {
        "id": ev_id,
        "indexed": True,
        "continuation_task_id": continuation.id if continuation else None,
    }


def _persist_and_notify_upload(
    run_id: str,
    title: str,
    extracted: document_ingest.ExtractedDocument,
    uploader: str,
) -> tuple[str, ScientificTask | None]:
    return _persist_attachment(
        NewEvidence(
            run_id=run_id,
            title=title,
            source=run_corpus.ATTACHMENT_SOURCE,
            abstract=extracted.text,
            mime_type=extracted.mime_type,
            sha256=extracted.sha256,
            byte_size=extracted.byte_size,
            document_version=extracted.sha256,
            extraction_tool=extracted.extraction_tool,
        ),
        uploader,
        f"Use the uploaded private research document '{title}' in subsequent work.",
    )


@attachments_router.post("/{run_id}/attachments/upload")
async def upload_attachment(
    run_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    consent: Annotated[bool, Form()],
) -> dict[str, Any]:
    """Extract and index a real scientist-uploaded document with provenance."""
    _require_run(run_id)
    uploader = client_id(request)
    if not consent:
        raise HTTPException(status_code=422, detail="consent is required to index a document")
    extracted = await extract_upload(file, owner=uploader or "")
    title = (file.filename or "Uploaded document").strip()
    evidence_id, continuation = _persist_and_notify_upload(run_id, title, extracted, uploader)
    return {
        "id": evidence_id,
        "indexed": True,
        "sha256": extracted.sha256,
        "byte_size": extracted.byte_size,
        "mime_type": extracted.mime_type,
        "extraction_tool": extracted.extraction_tool,
        "continuation_task_id": continuation.id if continuation else None,
    }


router = APIRouter()
router.include_router(attachments_router)
