from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import co_scientist.domains.chat.qa.manifest as qa_run_state
from co_scientist.api.auth import require_client_scope
from co_scientist.api.contracts import MessagesResponse
from co_scientist.api.contracts.interviews import Interview
from co_scientist.api.contracts.runs import RunMessage
from co_scientist.api.launch_admission import chat_response
from co_scientist.api.runs.models import (
    AskRequest,
    QaRevisionRequest,
    SendMessageRequest,
    StartAnnouncementRequest,
)
from co_scientist.api.runs.support import (
    _require_run,
    _run_or_404,
    prepare_continuation,
    request_continuation,
)
from co_scientist.core import byok_scope
from co_scientist.core.async_bridge import off_loop
from co_scientist.domains.access import credentials
from co_scientist.domains.chat import qa, run_start_announcement
from co_scientist.domains.chat.qa import snapshot
from co_scientist.domains.chat.repository import messages as store
from co_scientist.domains.chat.repository.examples import open_example_chat
from co_scientist.domains.chat.repository.messages import NewMessage
from co_scientist.domains.research_state.repository import hypotheses as store_hypotheses
from co_scientist.domains.research_state.repository import records
from co_scientist.platform import db
from co_scientist.platform.db.launch_control import LaunchControl, require_unpaused
from co_scientist.platform.db.models import MessageRow, RunRow, RunStatus
from co_scientist.platform.llm.process_mode import offline_mode

router = APIRouter()


@router.post("/{run_id}/messages", response_model=RunMessage)
@off_loop
def send_message(run_id: str, req: SendMessageRequest) -> dict[str, Any]:
    """Queue scientist steering and continue a completed engine run."""
    _require_run(run_id)
    admission = prepare_continuation(run_id)
    with db.transaction() as conn:
        msg = store.append_message(
            NewMessage(run_id=run_id, sender="user", content=req.content, kind="steering"),
            conn=conn,
        )
        continuation = request_continuation(run_id, msg.id, conn=conn, admission=admission)
    return {
        **msg.to_dict(),
        "status": "queued",
        "continuation_task_id": continuation.id if continuation else None,
    }


@router.get("/{run_id}/messages", response_model=MessagesResponse)
@off_loop
def list_messages(run_id: str) -> dict[str, Any]:
    """Return all messages for a run in chronological order."""
    _require_run(run_id)
    msgs = store.list_messages(run_id)
    return {"messages": [m.to_dict() for m in msgs]}


def _gather_qa_context(run: RunRow) -> qa.QaRunContext:
    """Q&A reads must not acquire SQLite's single writer."""
    with db.connect() as conn:
        conn.execute("BEGIN")
        state = snapshot.checkpoint_state(run, conn)
        hypotheses = store_hypotheses.list_hypotheses(run.id, conn=conn)
        if "hypotheses" in state and run.status != RunStatus.COMPLETED:
            hypotheses = [snapshot.idea_view(h) for h in state["hypotheses"] if isinstance(h, dict)]
        reviews = records.list_reviews(run.id, conn=conn)
        if "hypotheses" in state and run.status != RunStatus.COMPLETED:
            reviews = [
                {
                    **r,
                    "reviewer_agent": r.get("reviewer", "review"),
                    "hypothesis_id": h.get("id", ""),
                    "summary": r.get("review_summary", ""),
                }
                for h in hypotheses
                for r in h.get("reviews") or []
                if isinstance(r, dict)
            ]
        matches = records.list_matches(run.id, conn=conn)
        if "tournament_matchups" in state and run.status != RunStatus.COMPLETED:
            matches = [
                {**match, "rationale": match.get("rationale") or match.get("reasoning") or ""}
                for match in state["tournament_matchups"] or []
                if isinstance(match, dict)
            ]
        history = store.list_messages(run.id, conn=conn)[:-1]
        evidence = records.list_evidence(run.id, conn=conn)
        citations = records.list_citations(run.id, conn=conn)
        artifacts = snapshot.gather_artifacts(
            run, conn, state, hypotheses, reviews, matches, history
        )
        progress = qa_run_state.gather_run_progress(
            run,
            reviews,
            {
                "ideas": len(hypotheses),
                "evidence": max(len(evidence), len(state.get("articles") or [])),
                "matches": len(matches),
            },
            conn,
            time.time(),
        )
    return qa.QaRunContext(
        research_goal=run.research_goal,
        artifacts=artifacts,
        hypotheses=hypotheses,
        reviews=reviews,
        matches=matches,
        history=history,
        manifest=qa.build_evidence_manifest(evidence, citations),
        progress=progress,
        report=(
            None
            if progress.is_running
            else (
                qa_run_state.build_report_facts(artifacts["report"][0])
                if artifacts.get("report")
                else None
            )
        ),
    )


def _offline_qa_response(
    run_id: str,
    question_msg: MessageRow,
    context: qa.QaRunContext,
) -> StreamingResponse:
    answer = qa.build_offline_answer(
        context.research_goal,
        context.hypotheses,
        context.reviews,
        context.manifest,
        question_msg.content,
    )
    return StreamingResponse(
        qa.stream_offline_answer(run_id, question_msg.id, answer, context.manifest),
        media_type="text/event-stream",
    )


def _request_byok(
    request: Request,
) -> byok_scope.ByokCredential | None:
    try:
        return credentials.credential_from_headers(request.headers)
    except credentials.ByokRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_qa_byok(run: RunRow, request: Request) -> byok_scope.ByokCredential | None:
    """Persisted run credentials take precedence so billing remains
    consistent across sessions.
    """
    byok = credentials.get_run_credential(run.id)
    return byok if byok is not None else _request_byok(request)


def _persist_question(run_id: str, content: str) -> tuple[MessageRow, LaunchControl]:
    """The question remains durable even if the response stream fails."""
    with db.transaction() as conn:
        control = require_unpaused(conn=conn)
        message = store.append_message(
            NewMessage(run_id=run_id, sender="user", content=content, kind="qa"), conn=conn
        )
    from co_scientist.platform.telemetry.diagnostic_events import log_chat_turn

    log_chat_turn("user", content, run_id=run_id)
    return message, control


def _live_qa_response(
    req: AskRequest,
    run: RunRow,
    question_msg: MessageRow,
    context: qa.QaRunContext,
    byok: byok_scope.ByokCredential | None,
) -> StreamingResponse:
    return StreamingResponse(
        qa.stream_answer(
            run.id,
            qa.QaQuestion(text=req.question, message_id=question_msg.id),
            qa.QaAnswerInputs(
                system_prompt=qa.build_system_prompt(context),
                manifest=context.manifest,
                ideas=context.hypotheses,
                artifacts=context.artifacts,
            ),
            byok=byok,
        ),
        media_type="text/event-stream",
    )


@router.post("/{run_id}/messages/ask")
@off_loop
def ask_question(run_id: str, req: AskRequest, request: Request) -> StreamingResponse:
    """Answer a question about the run using a fast LLM.

    The response is streamed back to the caller.
    """
    run = _run_or_404(run_id)
    byok = _resolve_qa_byok(run, request)
    question_msg, control = _persist_question(run_id, req.question)

    context = _gather_qa_context(run)
    if offline_mode() and byok is None:
        return chat_response(_offline_qa_response(run_id, question_msg, context), control=control)
    return chat_response(_live_qa_response(req, run, question_msg, context, byok), control=control)


@router.post("/{run_id}/messages/{message_id}/revise")
@off_loop
def revise_question(
    run_id: str, message_id: int, req: QaRevisionRequest, request: Request
) -> StreamingResponse:
    run = _run_or_404(run_id)
    byok = _resolve_qa_byok(run, request)
    try:
        with db.transaction() as conn:
            control = require_unpaused(conn=conn)
            question_msg = store.rewind_qa(run_id, message_id, req.question, conn=conn)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from co_scientist.platform.telemetry.diagnostic_events import log_chat_turn

    log_chat_turn("user", question_msg.content, run_id=run_id)
    context = _gather_qa_context(run)
    if offline_mode() and byok is None:
        return chat_response(_offline_qa_response(run_id, question_msg, context), control=control)
    return chat_response(
        _live_qa_response(
            AskRequest(question=question_msg.content),
            run,
            question_msg,
            context,
            byok,
        ),
        control=control,
    )


@router.post("/{run_id}/messages/started")
@off_loop
def announce_start(
    run_id: str, req: StartAnnouncementRequest, request: Request
) -> StreamingResponse:
    """Answer the scientist's start request in the Agent's own words.

    The reply the chat shows above the session card. It is a turn, not a
    notice: the scientist's prompt is persisted first (so the exchange
    survives a reload whatever the reply does), then the announcement
    streams and is persisted in turn. A provider this call cannot reach
    ends in the deterministic announcement rather than an error -- the run
    has already started by the time this endpoint is reached, and nothing
    here can change that.
    """
    run = _run_or_404(run_id)
    byok = _resolve_qa_byok(run, request)
    with db.transaction() as conn:
        control = require_unpaused(conn=conn)
        prompt_msg, fresh = store.claim_start_prompt(run_id, req.prompt, conn=conn)
    if not fresh:
        return chat_response(
            StreamingResponse(
                run_start_announcement.replay_announcement(run_id, prompt_msg.id),
                media_type="text/event-stream",
            ),
            control=control,
        )
    from co_scientist.platform.telemetry.diagnostic_events import log_chat_turn

    log_chat_turn("user", req.prompt, run_id=run_id)
    return chat_response(
        StreamingResponse(
            run_start_announcement.stream_announcement(run, prompt_msg.id, byok),
            media_type="text/event-stream",
        ),
        control=control,
    )


@router.post("/{run_id}/example-chat", response_model=Interview)
@off_loop
def open_example(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return open_example_chat(run_id, require_client_scope(request))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
