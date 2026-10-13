from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from co_scientist.core.run_modes import normalize_run_tier
from co_scientist.domains.report import ReportRequest
from co_scientist.domains.report import repository as reports
from co_scientist.domains.safety.gate import SafetyDecision
from co_scientist.orchestration.drain import persist_final_state
from co_scientist.orchestration.engine_tasks import runtime as engine_tasks_runtime
from co_scientist.orchestration.engine_tasks.inputs import reopen_for_pending_scientist_input
from co_scientist.orchestration.engine_tasks.report_finalize import finalize_report
from co_scientist.orchestration.engine_tasks.support import (
    FINALIZE_TASK,
    SafetyHoldError,
    TaskCommit,
    _latest_task_checkpoint,
    _metrics_snapshot,
    _plain_final_state,
    _require_run,
    _save_paused_checkpoint,
    _save_paused_state_if_requested,
    assert_task_commit_allowed,
    restore_checkpoint_state,
)
from co_scientist.orchestration.repository import events
from co_scientist.orchestration.repository import runs_views as views
from co_scientist.orchestration.run_events import make_emitter
from co_scientist.orchestration.safety_gate import apply_safety_gate
from co_scientist.platform import db
from co_scientist.platform.db import retrieval_calls as retrieval
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunRow, RunStatus, ScientificTask


def _finalize_replay_or_none(run: RunRow, *, db_path: str | None) -> dict[str, Any] | None:
    if run.status == RunStatus.CANCELLED.value:
        raise RuntimeError("run cancelled before finalization")
    already_published = (
        run.status == RunStatus.COMPLETED.value
        and reports.get_latest_report(run.id, db_path=db_path) is not None
    )
    if already_published:
        return {"run_id": run.id, "status": "completed", "replayed": True}
    return None


def _settle_finalize_outcome(run_id: str, db_path: str | None) -> dict[str, Any]:
    run = runs.get_run(run_id, db_path=db_path)
    status = run.status if run else "missing"
    if (
        status == RunStatus.PAUSED.value
        and reports.get_latest_report(run_id, db_path=db_path) is None
    ):
        raise SafetyHoldError("report finalization held for review")
    return {"run_id": run_id, "status": status}


def _pause_finalize_if_requested(
    commit: TaskCommit, state: dict[str, Any]
) -> dict[str, Any] | None:
    run = runs.get_run(commit.task.run_id, db_path=commit.db_path)
    if run is None or run.status != RunStatus.PAUSED.value:
        return None
    checkpoint_seq = _save_paused_state_if_requested(commit, state, FINALIZE_TASK)
    if checkpoint_seq is None:
        return None
    return {"checkpoint_seq": checkpoint_seq, "status": "paused"}


def _commit_finalize_drain(
    commit: TaskCommit,
    state: dict[str, Any],
    drained: Any,
    metrics: dict[str, Any],
) -> dict[str, Any] | None:
    from co_scientist.orchestration.checkpoint import serialize_workflow_state

    task, db_path = commit.task, commit.db_path
    envelope = serialize_workflow_state(state, last_event_seq=0)
    with db.transaction(db_path) as conn:
        assert_task_commit_allowed(task, conn)
        status = conn.execute("SELECT status FROM runs WHERE id=?", (task.run_id,)).fetchone()[
            "status"
        ]
        if status == RunStatus.PAUSED.value:
            views.clear_publication_artifacts(task.run_id, conn=conn)
            envelope["last_event_seq"] = events.latest_event_seq(task.run_id, conn=conn)
            checkpoint_seq = _save_paused_checkpoint(
                commit,
                state,
                FINALIZE_TASK,
                envelope,
                conn,
            )
            retrieval.save_run_metrics(task.run_id, metrics, conn=conn)
            return {"checkpoint_seq": checkpoint_seq, "status": "paused"}
        retrieval.save_run_metrics(task.run_id, metrics, conn=conn)
        runs.update_run_status(task.run_id, RunStatus.SYNTHESIZING, conn=conn)
        for event_type, payload in _finalize_stage_events(drained):
            events.append_event(task.run_id, event_type, payload, conn=conn)
    return None


def _finalize_stage_events(
    drained: Any,
) -> Iterator[tuple[str, dict[str, Any]]]:
    yield "safety.hypothesis", drained.safety_counts
    yield "citation.grounding", drained.grounding_counts
    yield "citation_audit", dict(drained.report_inputs["citation_summary"])


def _monitor_halt_decision(state: dict[str, Any]) -> SafetyDecision:
    """A missing audit record cannot undo a safety halt; the flag decides
    admission and the record supplies detail.
    """
    from co_scientist.domains.safety.monitor import MONITOR_STAGE

    records = [
        item
        for item in (state.get("safety_decisions") or [])
        if isinstance(item, dict) and item.get("stage") == MONITOR_STAGE
    ]
    record: dict[str, Any] = records[-1] if records else {}
    return SafetyDecision(
        stage=MONITOR_STAGE,
        decision="block",
        reason=str(
            record.get("reason") or "The research direction reached prohibited content mid-run."
        ),
        matches=[str(match) for match in (record.get("matches") or [])],
        category=str(record.get("outcome") or "prohibited"),
        assessor="engine:safety_monitor",
    )


async def _halt_finalize_if_blocked(
    run: RunRow,
    state: dict[str, Any],
    task: ScientificTask,
    db_path: str | None,
) -> dict[str, Any] | None:
    """A monitor-halted run publishes nothing and spends no synthesis merely
    to withhold it later.
    """
    if not state.get("safety_blocked"):
        return None
    decision = _monitor_halt_decision(state)
    emit = make_emitter(run.id, db_path=db_path)
    async for _ in apply_safety_gate(run.id, decision, emit, db_path=db_path, task=task):
        pass
    return {"run_id": run.id, "status": RunStatus.BLOCKED.value}


async def _drain_and_persist_final_state(
    run: RunRow,
    state: dict[str, Any],
    db_path: str | None,
) -> tuple[Any, float, dict[str, Any]]:
    final_state = _plain_final_state(state)
    views.clear_publication_artifacts(run.id, db_path=db_path)
    drained = await persist_final_state(run_id=run.id, final_state=final_state, db_path=db_path)
    metrics = _metrics_snapshot(final_state)
    execution_time = max(0.0, time.time() - float(state.get("start_time", 0)))
    return drained, execution_time, metrics


def _restore_finalize_checkpoint(
    task: ScientificTask, db_path: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    checkpoint, _ = _latest_task_checkpoint(task, db_path)
    return checkpoint, restore_checkpoint_state(task, checkpoint, db_path)


async def _publish_finalize_report(
    run: RunRow,
    task: ScientificTask,
    drained: Any,
    execution_time: float,
    emit: Any,
    db_path: str | None,
) -> None:
    setup = run.config.get("setup") if isinstance(run.config, dict) else None
    async for _ in finalize_report(
        run.id,
        ReportRequest(
            research_goal=run.research_goal,
            goal_restatement=run.goal_restatement,
            run_mode=normalize_run_tier(run.profile),
            provider="engine",
            execution_time=execution_time,
            setup=setup if isinstance(setup, dict) else None,
            prepared_at=time.time(),
            db_path=db_path,
            **drained.report_inputs,
        ),
        emit,
        task=task,
    ):
        pass


async def execute_finalize(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
    run = _require_run(task, db_path)
    replayed = _finalize_replay_or_none(run, db_path=db_path)
    if replayed is not None:
        return replayed
    checkpoint, state = _restore_finalize_checkpoint(task, db_path)
    commit = TaskCommit(task, int(checkpoint["seq"]), db_path)
    paused = _pause_finalize_if_requested(commit, state)
    if paused is not None:
        return paused
    halted = await _halt_finalize_if_blocked(run, state, task, db_path)
    if halted is not None:
        return halted
    drain = engine_tasks_runtime.active().drain_final_state
    drained, execution_time, metrics = await drain(run, state, db_path)
    paused = _commit_finalize_drain(commit, state, drained, metrics)
    if paused is not None:
        return paused
    emit = make_emitter(run.id, db_path=db_path)
    await _publish_finalize_report(run, task, drained, execution_time, emit, db_path)
    # Input posted during publication has no continuation task yet; reopen
    # completed runs with pending contributions.
    reopen_for_pending_scientist_input(run.id, db_path=db_path)
    return _settle_and_release(run.id, db_path)


def _settle_and_release(run_id: str, db_path: str | None) -> dict[str, Any]:
    """Call-budget tracking is released on nonterminal exits as well as
    final settlement.
    """
    from co_scientist.platform.llm import release_run_call_budget

    outcome = _settle_finalize_outcome(run_id, db_path)
    release_run_call_budget(run_id)
    return outcome
