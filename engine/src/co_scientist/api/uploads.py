from __future__ import annotations

import asyncio

from fastapi import HTTPException, UploadFile

from co_scientist.domains.documents import extraction_admission, ingest
from co_scientist.platform.db.storage_admission import current_peer


async def extract_upload(file: UploadFile, *, owner: str = "") -> ingest.ExtractedDocument:
    peer = current_peer()
    if not extraction_admission.acquire(owner, peer):
        raise HTTPException(status_code=429, detail="document extraction capacity reached")
    submitted = False
    try:
        data = await file.read(ingest.MAX_UPLOAD_BYTES + 1)

        def extract() -> ingest.ExtractedDocument:
            try:
                return ingest.extract_document(
                    data, file.content_type or "application/octet-stream"
                )
            finally:
                # Cancelling the HTTP await does not stop a running parser.
                extraction_admission.release(owner, peer)

        future = asyncio.get_running_loop().run_in_executor(None, extract)
        submitted = True
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(future)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if not submitted:
            extraction_admission.release(owner, peer)
