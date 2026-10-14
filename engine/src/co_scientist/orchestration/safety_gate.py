from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.records import NewSafetyDecision
from co_scientist.domains.safety.types import SafetyDecision, redact_matched_spans
from co_scientist.orchestration.repository import events as store_events
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus, ScientificTask

logger = logging.getLogger(__name__)


def _apply_intake_redaction(
    run_id: str,
    result: SafetyDecision,
    *,
    db_path: str | None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Apply redaction to stored goal and derived title inside the gate; an
    audit label alone leaves unsafe content readable.
    """
    if result.stage != "intake" or result.decision != "redact":
        return
    run = runs.get_run(run_id, db_path=db_path, conn=conn)
    if run is None:
        return
    matches = list(result.matches)
    runs.redact_run_goal(
        run_id,
        redact_matched_spans(run.research_goal, matches),
        redact_matched_spans(run.title or "", matches),
        db_path=db_path,
        conn=conn,
    )
    logger.warning(
        "Redacted %d matched span(s) from run %s's goal at the intake gate.",
        len(matches),
        run_id,
    )


def _record_safety_decision(
    run_id: str,
    result: SafetyDecision,
    *,
    db_path: str | None,
    conn: sqlite3.Connection | None = None,
) -> None:
    records.add_safety_decision(
        NewSafetyDecision(
            run_id=run_id,
            stage=result.stage,
            decision=result.decision,
            reason=result.reason,
            matches=result.matches,
            category=result.category,
            policy_version=result.policy_version,
            risk_domains=result.risk_domains,
            requires_review=result.requires_review,
            assessor=result.assessor,
        ),
        db_path=db_path,
        conn=conn,
    )
    if result.decision in {"block", "hold"}:
        logger.warning(
            "Safety gate withheld run %s at %s stage: %s",
            run_id,
            result.stage,
            result.reason,
        )
    else:
        logger.info(
            "Safety gate %s run %s at %s stage.",
            result.decision,
            run_id,
            result.stage,
        )


def _assert_bootstrap_intake_lease(
    run_id: str, task: ScientificTask, conn: sqlite3.Connection
) -> None:
    if task.run_id == run_id and runs.bootstrap_task_lease_matches(
        conn, run_id, task.id, task.lease_owner, task.attempt
    ):
        return
    from co_scientist.orchestration.task_worker.outcomes import LeaseLostError

    raise LeaseLostError(f"bootstrap task {task.id} lost its lease before intake commit")


def _event_record(seq: int, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"seq": seq, "type": event_type, "payload": payload}


def _commit_task_safety_events(
    run_id: str,
    result: SafetyDecision,
    task: ScientificTask,
    db_path: str | None,
) -> list[dict[str, Any]]:
    from co_scientist.orchestration.engine_tasks.support import assert_task_commit_allowed

    decision_payload = result.to_dict()
    event_type = f"safety.{result.stage}"
    with db.transaction(db_path) as conn:
        assert_task_commit_allowed(task, conn)
        if result.stage == "intake":
            _assert_bootstrap_intake_lease(run_id, task, conn)
        _record_safety_decision(run_id, result, db_path=db_path, conn=conn)
        _apply_intake_redaction(run_id, result, db_path=db_path, conn=conn)
        seq = store_events.append_event(run_id, event_type, decision_payload, conn=conn)
        events = [_event_record(seq, event_type, decision_payload)]
        if result.decision == "block":
            status = RunStatus.BLOCKED
            payload = {"status": "blocked", "error": result.reason}
        elif result.decision == "hold":
            status = RunStatus.PAUSED
            payload = {
                "status": "paused",
                "reason": "safety_review",
                "error": result.reason,
            }
        else:
            return events
        runs.update_run_status(run_id, status, error=result.reason, db_path=db_path, conn=conn)
        seq = store_events.append_event(run_id, "status", payload, conn=conn)
        events.append(_event_record(seq, "status", payload))
    return events


def _commit_gate_status_event(
    run_id: str,
    status: RunStatus,
    error: str | None,
    payload: dict[str, Any],
    *,
    status_guard: tuple[str | None, tuple[str, str | None, int] | None],
) -> dict[str, Any] | None:
    """Transition and terminal event share one transaction so cancellation
    cannot be followed by a late safety-stop event.
    """
    active_statuses = (
        RunStatus.DRAFT,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.SYNTHESIZING,
        RunStatus.PAUSED,
    )
    db_path, lease_guard = status_guard
    with db.transaction(db_path) as conn:
        if lease_guard is not None and not runs.bootstrap_task_lease_matches(
            conn, run_id, *lease_guard
        ):
            return None
        changed = runs.update_run_status_if_current(conn, run_id, status, active_statuses, error)
        if not changed:
            return None
        seq = store_events.append_event(run_id, "status", payload, conn=conn)
    return {"seq": seq, "type": "status", "payload": payload}


async def _yield_terminal_status_event(
    run_id: str,
    result: SafetyDecision,
    *,
    db_path: str | None,
    lease_guard: tuple[str, str | None, int] | None,
) -> AsyncIterator[dict[str, Any]]:
    if result.decision == "block":
        event = _commit_gate_status_event(
            run_id,
            RunStatus.BLOCKED,
            result.reason,
            {"status": "blocked", "error": result.reason},
            status_guard=(db_path, lease_guard),
        )
        if event is not None:
            yield event
    elif result.decision == "hold":
        payload = {
            "status": "paused",
            "reason": "safety_review",
            "error": result.reason,
        }
        event = _commit_gate_status_event(
            run_id,
            RunStatus.PAUSED,
            result.reason,
            payload,
            status_guard=(db_path, lease_guard),
        )
        if event is not None:
            yield event


async def apply_safety_gate(
    run_id: str,
    result: SafetyDecision,
    emit: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    db_path: str | None = None,
    lease_guard: tuple[str, str | None, int] | None = None,
    task: ScientificTask | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Callers must stop workflow execution after a block; emitting a
    terminal verdict cannot itself stop the caller.
    """
    if task is not None:
        events = _commit_task_safety_events(run_id, result, task, db_path)
        for event in events:
            yield event
        return
    _record_safety_decision(run_id, result, db_path=db_path)
    _apply_intake_redaction(run_id, result, db_path=db_path)
    yield await emit(f"safety.{result.stage}", result.to_dict())
    async for event in _yield_terminal_status_event(
        run_id, result, db_path=db_path, lease_guard=lease_guard
    ):
        yield event
