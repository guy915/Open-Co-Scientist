from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from fastapi import HTTPException, Request

import co_scientist.domains.access.credentials as credentials
import co_scientist.domains.chat.interviews.model as model
import co_scientist.domains.chat.interviews.questions as question_repair
import co_scientist.domains.documents.staged as staged_documents
from co_scientist.api.auth import client_id
from co_scientist.api.documents_access import resolve_owned_documents
from co_scientist.core import byok_scope
from co_scientist.domains.chat.interviews.model import (
    ProseSink,
    ReasoningSink,
    _essentials_ready,
    _normalized_fields,
    _ready,
)
from co_scientist.domains.chat.interviews.questions import normalized_questions
from co_scientist.domains.chat.repository import interviews as store
from co_scientist.domains.chat.repository.interviews import NewInterviewTurn
from co_scientist.domains.documents import repository as documents
from co_scientist.platform.db import Connection
from co_scientist.platform.llm.llm_scope import budgeted
from co_scientist.platform.llm.process_mode import offline_mode

logger = logging.getLogger(__name__)


def owned_interview(interview_id: str, request: Request) -> dict[str, Any]:
    """An empty identity owns nothing, including historical rows with empty
    ownership.
    """
    interview = store.get_interview(interview_id)
    subject = client_id(request)
    if not subject or interview is None or interview["client_id"] != subject:
        raise HTTPException(status_code=404, detail="interview not found")
    return _with_documents(interview)


def request_byok(request: Request) -> byok_scope.ByokCredential | None:
    """Per-request interview credentials override run defaults and are never
    persisted.
    """
    try:
        credential = credentials.credential_from_headers(request.headers)
    except credentials.ByokRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return credential


def _with_documents(interview: dict[str, Any]) -> dict[str, Any]:
    """Responses expose attachment metadata, not full private document text."""
    attached = documents.list_interview_documents(str(interview["id"]))
    interview["documents"] = [staged_documents.document_summary(d) for d in attached]
    return interview


def _attach_documents(
    interview_id: str,
    document_ids: list[str],
    request: Request,
    *,
    conn: Connection | None = None,
) -> None:
    if not document_ids:
        return
    owner = client_id(request)
    resolve_owned_documents(document_ids, owner, conn=conn)
    documents.attach_documents_to_interview(interview_id, document_ids, owner, conn=conn)


def _reasoning_capture(
    on_reasoning: ReasoningSink | None,
) -> tuple[ReasoningSink, list[str]]:
    fragments: list[str] = []

    async def sink(fragment: str) -> None:
        fragments.append(fragment)
        if on_reasoning is not None:
            await on_reasoning(fragment)

    return sink, fragments


@budgeted("interview")
async def advance_turn(
    interview_id: str,
    on_reasoning: ReasoningSink | None = None,
    on_prose: ProseSink | None = None,
) -> dict[str, Any]:
    interview = store.get_interview(interview_id)
    assert interview is not None
    started = time.perf_counter()
    sink, fragments = _reasoning_capture(on_reasoning)
    response, used_fallback = await _run_interview_turn(interview, sink, on_prose)
    turn = await _with_repaired_questions(
        _resolved_turn(response, used_fallback, "".join(fragments))
    )
    _persist_interview_turn(interview_id, turn)
    from co_scientist.platform.telemetry.diagnostic_events import log_chat_turn

    log_chat_turn(
        "agent",
        turn.message,
        owner=interview["client_id"],
        duration_seconds=time.perf_counter() - started,
    )
    updated = store.get_interview(interview_id)
    assert updated is not None
    return _with_documents(updated)


@dataclasses.dataclass(frozen=True)
class _ResolvedTurn:
    """Fields accumulate across turns; questions and fallback provenance
    belong only to the current turn.
    """

    message: str
    fields: dict[str, Any]
    reasoning: str
    completed: bool
    fallback: bool
    questions: list[dict[str, Any]]


def _resolved_turn(response: dict[str, Any], used_fallback: bool, reasoning: str) -> _ResolvedTurn:
    fields = _normalized_fields(response)
    message = str(response.get("assistant_message") or "").strip()
    if not message:
        raise HTTPException(status_code=502, detail="Interview Agent returned no message.")
    return _ResolvedTurn(
        message=message,
        fields=fields,
        reasoning=reasoning,
        completed=_interview_turn_completed(response, fields, used_fallback),
        fallback=used_fallback,
        questions=normalized_questions(response.get("questions")),
    )


async def _with_repaired_questions(turn: _ResolvedTurn) -> _ResolvedTurn:
    """Repair recovers already-asked prose questions only; it cannot invent
    questions or alter completed turns.
    """
    if turn.questions or turn.completed or turn.fallback:
        return turn
    questions = await question_repair.repair_questions(turn.message)
    if not questions:
        return turn
    logger.info("Recovered %d interview question(s) from prose", len(questions))
    return dataclasses.replace(turn, questions=questions)


def _persist_interview_turn(interview_id: str, turn: _ResolvedTurn) -> None:
    store.append_interview_turn(
        interview_id,
        NewInterviewTurn(
            "agent",
            turn.message,
            turn.reasoning,
            fallback=turn.fallback,
            questions=turn.questions,
        ),
    )
    store.update_interview(
        interview_id,
        turn.fields,
        None if turn.completed else turn.message,
        completed=turn.completed,
    )


async def _run_interview_turn(
    interview: dict[str, Any],
    on_reasoning: ReasoningSink | None,
    on_prose: ProseSink | None = None,
) -> tuple[dict[str, Any], bool]:
    """Fallback provenance is resolved per turn because credentials or
    provider availability can change.
    """
    try:
        response = await model._call_interview_model(interview, on_reasoning, on_prose)
        return response, False
    except model.InterviewModelUnavailableError as error:
        if not offline_mode():
            raise HTTPException(503, "No model is available right now") from error
        return model._fallback_interview_response(interview), True


def _interview_turn_completed(
    response: dict[str, Any], fields: dict[str, Any], used_fallback: bool
) -> bool:
    if used_fallback:
        return _ready(fields)
    # Explicitly empty preferences are valid and must not deadlock completion.
    return bool(response.get("completed")) and _essentials_ready(fields)
