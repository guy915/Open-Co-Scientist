from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import co_scientist.api.interviews.turns as support
from co_scientist.api.auth import client_id, require_client_scope
from co_scientist.api.contracts.interviews import ChatSummary, Interview
from co_scientist.api.documents_access import resolve_owned_documents
from co_scientist.api.interviews.models import (
    CreateInterviewRequest as CreateInterviewRequest,
)
from co_scientist.api.interviews.models import (
    InterviewFieldsRequest as InterviewFieldsRequest,
)
from co_scientist.api.interviews.models import InterviewTurnRequest
from co_scientist.api.interviews.stream import _interview_stream
from co_scientist.api.interviews.turns import _attach_documents as _attach_documents
from co_scientist.api.interviews.turns import (
    _interview_turn_completed as _interview_turn_completed,
)
from co_scientist.api.interviews.turns import (
    _persist_interview_turn as _persist_interview_turn,
)
from co_scientist.api.interviews.turns import _reasoning_capture as _reasoning_capture
from co_scientist.api.interviews.turns import _resolved_turn as _resolved_turn
from co_scientist.api.interviews.turns import _ResolvedTurn as _ResolvedTurn
from co_scientist.api.interviews.turns import _run_interview_turn as _run_interview_turn
from co_scientist.api.interviews.turns import _with_documents as _with_documents
from co_scientist.api.interviews.turns import (
    _with_repaired_questions as _with_repaired_questions,
)
from co_scientist.api.interviews.turns import advance_turn as advance_turn
from co_scientist.api.interviews.turns import owned_interview as _owned_interview
from co_scientist.api.interviews.turns import request_byok as _request_byok
from co_scientist.core.async_bridge import off_loop
from co_scientist.domains.chat.interviews.model import ProseSink as ProseSink
from co_scientist.domains.chat.interviews.model import ReasoningSink as ReasoningSink
from co_scientist.domains.chat.interviews.model import (
    _call_interview_model as _call_interview_model,
)
from co_scientist.domains.chat.interviews.model import _clean_list as _clean_list
from co_scientist.domains.chat.interviews.model import _essentials_ready as _essentials_ready
from co_scientist.domains.chat.interviews.model import (
    _fallback_interview_response as _fallback_interview_response,
)
from co_scientist.domains.chat.interviews.model import _normalized_fields as _normalized_fields
from co_scientist.domains.chat.interviews.model import _ready as _ready
from co_scientist.domains.chat.interviews.questions import (
    normalized_questions as normalized_questions,
)
from co_scientist.domains.chat.interviews.questions import repair_questions as repair_questions
from co_scientist.domains.chat.repository import interviews as store
from co_scientist.domains.chat.repository.interviews import NewInterviewTurn
from co_scientist.platform.db import Connection, transaction
from co_scientist.platform.db.launch_control import require_unpaused
from co_scientist.platform.telemetry.diagnostic_events import log_chat_turn

_revision_router = APIRouter()


def _require_revisable_turn(interview: dict[str, Any], turn_id: int, role: str) -> None:
    if store.run_id_for_interview(str(interview["id"]), str(interview["client_id"])):
        raise HTTPException(status_code=409, detail="setup has already been consumed by a run")
    if interview["status"] == "cancelled":
        raise HTTPException(status_code=409, detail="interview is cancelled")
    turn = next((t for t in interview["turns"] if int(t["id"]) == turn_id), None)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found")
    if turn["role"] != role:
        raise HTTPException(status_code=409, detail=f"turn is not a {role} turn")


def _reset_derivation(interview_id: str, conn: Connection) -> None:
    """Rewinding a transcript withdraws derived fields and rebaselines the
    surviving conversation.
    """
    interview = store.get_interview(interview_id, conn=conn)
    assert interview is not None
    opening = next((t for t in interview["turns"] if t["role"] == "user"), None)
    store.update_interview(
        interview_id,
        {
            "research_challenge": str(opening["content"]) if opening else "",
            "focus_area": [],
            "preferences": [],
            "lab_constraints": [],
            "title": None,
        },
        "Continue the interview.",
        completed=False,
        conn=conn,
    )


def _rewind_and_restream(
    interview_id: str,
    turn_id: int,
    request: Request,
    *,
    role: str,
    replacement: str | None = None,
) -> StreamingResponse:
    """Edit and retry share ownership, revisability, rewind and derivation
    boundaries.
    """
    interview = support.owned_interview(interview_id, request)
    byok = support.request_byok(request)
    _require_revisable_turn(interview, turn_id, role)
    with transaction() as conn:
        control = require_unpaused(conn=conn)
        store.rewind_interview(interview_id, turn_id, conn=conn)
        if replacement is not None:
            store.append_interview_turn(
                interview_id, NewInterviewTurn("user", replacement), conn=conn
            )
        _reset_derivation(interview_id, conn)
    if replacement is not None:
        log_chat_turn("user", replacement, owner=client_id(request))
    return _interview_stream(interview_id, byok, control=control)


@_revision_router.put("/{interview_id}/turns/{turn_id}")
@off_loop
def edit_interview_turn(
    interview_id: str,
    turn_id: int,
    body: InterviewTurnRequest,
    request: Request,
) -> StreamingResponse:
    """Replace one scientist turn in place and re-answer from there.

    An edited prompt is a correction, not a new question: the turn is
    rewritten where it stands and the Agent answers it again, rather than the
    old wording staying in the transcript with the correction appended after
    it -- which is what makes the two readings of "what did I ask?" disagree.
    Everything the Agent said after it was derived from the old wording, so
    it goes with it.
    """
    return _rewind_and_restream(
        interview_id, turn_id, request, role="user", replacement=body.content
    )


@_revision_router.post("/{interview_id}/turns/{turn_id}/retry")
@off_loop
def retry_interview_turn(interview_id: str, turn_id: int, request: Request) -> StreamingResponse:
    """Discard one Agent turn and answer the same prompt again.

    Retry has to remove the answer it is replacing. Re-running the model
    with the rejected turn still in the transcript asks it to continue from
    the answer rather than to reconsider it.
    """
    return _rewind_and_restream(interview_id, turn_id, request, role="agent")


router = APIRouter(prefix="/api/interviews", tags=["interviews"])


@router.post("")
@off_loop
def create_interview(body: CreateInterviewRequest, request: Request) -> StreamingResponse:
    """Start a durable Agent interview and stream its opening turn.

    Any documents named in the body are attached before the opening turn is
    derived, so the Agent's first question is already scoped by them.

    Raises:
        HTTPException: 400 when the caller carries no identity at all (see
            ``co_scientist.api.auth.require_client_scope``) -- checked first, since an
            interview created under that scope would be invisible to its
            own creator.
    """
    owner = require_client_scope(request)
    byok = _request_byok(request)
    # Reject invalid attachment IDs before creating a row, avoiding orphan
    # interviews.
    resolve_owned_documents(body.document_ids, owner)
    with transaction() as conn:
        control = require_unpaused(conn=conn)
        interview = store.create_interview(owner, body.research_challenge, conn=conn)
        _attach_documents(str(interview["id"]), body.document_ids, request, conn=conn)
    log_chat_turn("user", body.research_challenge, owner=owner)
    return _interview_stream(str(interview["id"]), byok, control=control)


@router.get("", response_model=list[ChatSummary])
@off_loop
def list_interviews(request: Request) -> list[dict[str, Any]]:
    """List the caller's chats, newest first, without their transcripts.

    Scoped to the calling client exactly as ``_owned_interview`` is: a chat
    carries a scientist's unfinished research goal, so it is never listed
    across clients. An identity-less caller's list is always empty, same
    as ``list_runs``.
    """
    subject = client_id(request)
    return store.list_interviews(subject) if subject else []


@router.get("/{interview_id}", response_model=Interview)
@off_loop
def get_interview(interview_id: str, request: Request) -> dict[str, Any]:
    """Resume an owned interview with its full transcript and progress.

    Carries ``run_id`` -- the run this chat started, when it started one.
    Reopening is the only place that needs it, and it needs it to know the
    plan is settled: without it the workbench re-staged the completing turn
    as an editable draft with Start research live, beside a card saying the
    run was already under way.
    """
    interview = _owned_interview(interview_id, request)
    interview["run_id"] = store.run_id_for_interview(interview_id, client_id(request))
    return interview


@router.delete("/{interview_id}")
@off_loop
def delete_interview(interview_id: str, request: Request) -> dict[str, Any]:
    """Permanently delete an owned chat and its transcript.

    Unlike a run (see ``co_scientist.api.runs.crud``) a chat has no worker that
    could be mid-write, so there is no active state to refuse: an
    interview is only ever advanced by a request the caller makes. A chat
    already carried into a run is still deletable, and deleting it leaves
    that run untouched -- the run holds its own copy of everything the
    interview contributed.

    Returns:
        The deleted chat's id and the row counts removed, per table.

    Raises:
        HTTPException: 404 if the chat does not exist or is not owned by
            the caller (the two are deliberately indistinguishable, see
            ``_owned_interview``).
    """
    _owned_interview(interview_id, request)
    counts = store.delete_interview(interview_id)
    return {"id": interview_id, "deleted": True, "counts": counts}


@router.post("/{interview_id}/turns")
@off_loop
def add_interview_turn(
    interview_id: str, body: InterviewTurnRequest, request: Request
) -> StreamingResponse:
    """Append a scientist answer and stream the Agent's next turn."""
    interview = _owned_interview(interview_id, request)
    byok = _request_byok(request)
    if interview["status"] != "active":
        raise HTTPException(status_code=409, detail="interview is not active")
    with transaction() as conn:
        control = require_unpaused(conn=conn)
        _attach_documents(interview_id, body.document_ids, request, conn=conn)
        store.append_interview_turn(interview_id, NewInterviewTurn("user", body.content), conn=conn)
    log_chat_turn("user", body.content, owner=client_id(request))
    return _interview_stream(interview_id, byok, control=control)


router.include_router(_revision_router)


@router.put("/{interview_id}/fields", response_model=Interview)
@off_loop
def edit_interview_fields(
    interview_id: str, body: InterviewFieldsRequest, request: Request
) -> dict[str, Any]:
    """Persist scientist edits and finalize when required fields are ready."""
    interview = _owned_interview(interview_id, request)
    fields = body.model_dump()
    for kept in ("title", "lab_constraints"):
        if kept not in body.model_fields_set:
            fields[kept] = interview["fields"].get(kept)
    fields["focus_area"] = _clean_list(fields["focus_area"])
    fields["preferences"] = _clean_list(fields["preferences"])
    fields["lab_constraints"] = _clean_list(fields["lab_constraints"] or [])
    completed = _ready(fields)
    with transaction() as conn:
        require_unpaused(conn=conn)
        store.update_interview(
            interview_id,
            fields,
            None if completed else "Continue the interview.",
            completed=completed,
            conn=conn,
        )
    updated = store.get_interview(interview_id)
    assert updated is not None
    return updated


__all__ = [
    "CreateInterviewRequest",
    "InterviewFieldsRequest",
    "InterviewTurnRequest",
    "ProseSink",
    "ReasoningSink",
    "_ResolvedTurn",
    "_attach_documents",
    "_call_interview_model",
    "_clean_list",
    "_essentials_ready",
    "_fallback_interview_response",
    "_interview_stream",
    "_interview_turn_completed",
    "_normalized_fields",
    "_persist_interview_turn",
    "_ready",
    "_reasoning_capture",
    "_resolved_turn",
    "_run_interview_turn",
    "_with_documents",
    "_with_repaired_questions",
    "advance_turn",
    "normalized_questions",
    "repair_questions",
]
