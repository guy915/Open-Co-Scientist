from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from co_scientist.domains.report import repository as store
from co_scientist.domains.report.build import (
    ReportRequest,
    _BuiltReport,
    build_report_content,
)
from co_scientist.domains.report.gates import _empty_leaderboard_reason
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.records import NewSafetyDecision
from co_scientist.domains.safety.gate import (
    SafetyDecision,
    ScreenSubject,
    redact_matched_spans,
    redact_payload_text,
    screen_final,
)
from co_scientist.orchestration.notifications import enqueue_completion_notification
from co_scientist.orchestration.repository import events
from co_scientist.orchestration.run_events import EmitFn
from co_scientist.orchestration.safety_gate import apply_safety_gate
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus, ScientificTask

logger = logging.getLogger(__name__)


async def finalize_report(
    run_id: str,
    req: ReportRequest,
    emit: EmitFn,
    *,
    resumed: bool = False,
    task: ScientificTask | None = None,
) -> AsyncIterator[dict[str, Any]]:
    logger.info("Finalizing report for run %s (provider=%s).", run_id, req.provider)
    if resumed and _report_already_published(run_id, db_path=req.db_path):
        return
    built, blocked, gate_events = await _build_and_gate_report(run_id, req, emit, task)
    for event in gate_events:
        yield event
    if blocked:
        return
    async for event in _gate_readiness_and_publish(run_id, req, emit, built, task):
        yield event


async def _gate_readiness_and_publish(
    run_id: str,
    req: ReportRequest,
    emit: EmitFn,
    built: _BuiltReport,
    task: ScientificTask | None,
) -> AsyncIterator[dict[str, Any]]:
    # Unsupported ideas publish as Unverified; an empty leaderboard means every
    # generated idea was withheld.
    if not built.payload.get("leaderboard"):
        async for event in _block_for_empty_leaderboard(
            run_id, built, emit, db_path=req.db_path, task=task
        ):
            yield event
        return
    async for event in _publish_report(
        run_id,
        req.research_goal,
        built,
        emit,
        db_path=req.db_path,
        task=task,
    ):
        yield event


async def _build_and_gate_report(
    run_id: str,
    req: ReportRequest,
    emit: EmitFn,
    task: ScientificTask | None,
) -> tuple[_BuiltReport, bool, list[dict[str, Any]]]:
    built = await build_report_content(run_id, req)
    final = await _screen_final_report(run_id, built.markdown, req.provider, db_path=req.db_path)
    if final.decision == "redact":
        built = _redacted_report(built, final)
    gate_events = [
        event
        async for event in apply_safety_gate(run_id, final, emit, db_path=req.db_path, task=task)
    ]
    blocked = final.decision in {"block", "hold"}
    if blocked:
        logger.warning(
            "Report finalize withheld for run %s by the final safety gate.",
            run_id,
        )
    return built, blocked, gate_events


def _redacted_report(built: _BuiltReport, decision: SafetyDecision) -> _BuiltReport:
    """Redact both payload and markdown: each is independently readable through
    reports and events.
    """
    matches = list(decision.matches)
    logger.warning(
        "Redacting %d matched span(s) from the report under the final safety gate.",
        len(matches),
    )
    return _BuiltReport(
        payload=redact_payload_text(built.payload, matches),
        markdown=redact_matched_spans(built.markdown, matches),
        # Claim facts are already public and unredacted via claim-evidence.
        facts=built.facts,
        exclusion_tally=built.exclusion_tally,
    )


def _report_already_published(run_id: str, *, db_path: str | None) -> bool:
    if store.get_latest_report(run_id, db_path=db_path) is None:
        return False
    logger.info(
        "Report already published for run %s; skipping duplicate finalize.",
        run_id,
    )
    return True


async def _screen_final_report(
    run_id: str,
    markdown: str,
    provider: str,
    *,
    db_path: str | None,
) -> SafetyDecision:
    from co_scientist.orchestration.engine_tasks import runtime as engine_tasks_runtime

    return await engine_tasks_runtime.active().screen(
        run_id,
        ScreenSubject("final", markdown, screen_final(markdown)),
        provider=provider,
        db_path=db_path,
    )


def _commit_leased_report_publication(
    run_id: str,
    research_goal: str,
    built: _BuiltReport,
    task: ScientificTask,
    db_path: str | None,
) -> tuple[dict[str, str], int, dict[str, Any], int, dict[str, Any]]:
    """Validate the finalize lease in the same transaction as publication
    writes.
    """
    # Keep imports local: engine task support closes a cycle through
    # engine_adapter and co_scientist.domains.report.
    from co_scientist.orchestration.engine_tasks.support import assert_task_commit_allowed

    with db.transaction(db_path) as conn:
        assert_task_commit_allowed(task, conn)
        saved = store.save_report(
            run_id,
            built.payload,
            built.markdown,
            db_path=db_path,
            conn=conn,
        )
        store.replace_knowledge_facts(run_id, built.facts, db_path=db_path, conn=conn)
        report_payload = {**built.payload, "report_id": saved["id"]}
        report_seq = events.append_event(run_id, "report", report_payload, conn=conn)
        status_payload = {"status": "completed"}
        status_seq = events.append_event(run_id, "status", status_payload, conn=conn)
        runs.update_run_status(run_id, RunStatus.COMPLETED, db_path=db_path, conn=conn)
        enqueue_completion_notification(
            run_id, research_goal, saved["id"], db_path=db_path, conn=conn
        )
    return saved, report_seq, report_payload, status_seq, status_payload


async def _publish_report(
    run_id: str,
    research_goal: str,
    built: _BuiltReport,
    emit: EmitFn,
    *,
    db_path: str | None,
    task: ScientificTask | None,
) -> AsyncIterator[dict[str, Any]]:
    payload = built.payload
    if task is None:
        saved = store.save_report(run_id, payload, built.markdown, db_path=db_path)
        # Write knowledge facts only on publication, never for blocked or held
        # reports.
        store.replace_knowledge_facts(run_id, built.facts, db_path=db_path)
        yield await emit("report", {**payload, "report_id": saved["id"]})
        runs.update_run_status(run_id, RunStatus.COMPLETED, db_path=db_path)
        enqueue_completion_notification(run_id, research_goal, saved["id"], db_path=db_path)
        yield await emit("status", {"status": "completed"})
    else:
        saved, report_seq, report_payload, status_seq, status_payload = (
            _commit_leased_report_publication(run_id, research_goal, built, task, db_path)
        )
        # The transaction already persisted events; emit stubs without writing
        # them twice.
        yield {"seq": report_seq, "type": "report", "payload": report_payload}
        yield {"seq": status_seq, "type": "status", "payload": status_payload}
    logger.info("Report finalized for run %s (report_id=%s).", run_id, saved["id"])


async def _block_for_empty_leaderboard(
    run_id: str,
    built: _BuiltReport,
    emit: EmitFn,
    *,
    db_path: str | None,
    task: ScientificTask | None,
) -> AsyncIterator[dict[str, Any]]:
    reason = _empty_leaderboard_reason(built.payload["idea_count"], built.exclusion_tally)
    decision = NewSafetyDecision(
        run_id=run_id,
        stage="scientific_readiness",
        decision="block",
        reason=reason,
        matches=[],
    )
    if task is None:
        records.add_safety_decision(decision, db_path=db_path)
        runs.update_run_status(run_id, RunStatus.BLOCKED, error=reason, db_path=db_path)
        event = await emit("status", {"status": "blocked", "reason": reason})
    else:
        seq = _commit_empty_leaderboard_block(run_id, decision, task, db_path)
        event = {
            "seq": seq,
            "type": "status",
            "payload": {"status": "blocked", "reason": reason},
        }
    logger.warning("Report finalize blocked for run %s: %s", run_id, reason)
    yield event


def _commit_empty_leaderboard_block(
    run_id: str,
    decision: NewSafetyDecision,
    task: ScientificTask,
    db_path: str | None,
) -> int:
    """Validate the finalize lease in the same transaction as readiness-block
    writes.
    """
    from co_scientist.orchestration.engine_tasks.support import assert_task_commit_allowed

    payload = {"status": "blocked", "reason": decision.reason}
    with db.transaction(db_path) as conn:
        assert_task_commit_allowed(task, conn)
        records.add_safety_decision(decision, conn=conn)
        runs.update_run_status(run_id, RunStatus.BLOCKED, error=decision.reason, conn=conn)
        return events.append_event(run_id, "status", payload, conn=conn)
