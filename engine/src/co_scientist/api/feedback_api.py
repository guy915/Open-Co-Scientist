from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from co_scientist.api.auth import require_client_scope
from co_scientist.core.async_bridge import off_loop
from co_scientist.domains.feedback import repository as feedback

router = APIRouter(tags=["feedback"])


class FeedbackRequest(BaseModel):
    category: feedback.Category
    message: str = Field(min_length=1, max_length=8_000)
    diagnostics: str = Field(max_length=100_000)
    url: str = Field(max_length=2_048)
    # Reported context only; this field never authorizes artifact reads.
    run_id: str | None = Field(None, max_length=128)

    @field_validator("message")
    @classmethod
    def nonempty_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a message is required")
        return value


@router.post("/api/feedback", status_code=201)
@off_loop
def submit_feedback(body: FeedbackRequest, request: Request) -> dict[str, str]:
    owner = require_client_scope(request)
    host = request.client.host if request.client else "unknown"
    host_key = hashlib.sha256(host.encode()).hexdigest()
    try:
        identity = feedback.submit(owner, host_key, feedback.Submission(**body.model_dump()))
    except feedback.RateExceededError as exc:
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)}
        ) from exc
    return {"id": identity, "status": "submitted"}
