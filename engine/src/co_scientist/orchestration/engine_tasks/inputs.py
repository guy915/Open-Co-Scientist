from __future__ import annotations

import logging
import sqlite3
from contextlib import nullcontext
from typing import Any

from co_scientist.core.config import settings
from co_scientist.core.constants import NEEDS_REVISION_SCORE, NOT_VIABLE_SCORE
from co_scientist.core.exceptions import ContinuationAdmissionError, ProviderAdmissionError
from co_scientist.core.run_modes import resolved_run_config
from co_scientist.domains.chat.repository import messages
from co_scientist.domains.research_state.elo import INITIAL_ELO
from co_scientist.domains.research_state.repository import hypotheses as store_hypotheses
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.safety.gate import ScreenSubject, screen_intake
from co_scientist.orchestration.engine_adapter import sync_engine_llm_backend
from co_scientist.orchestration.engine_tasks import runtime as engine_tasks_runtime
from co_scientist.orchestration.engine_tasks.support import (
    BOOTSTRAP_TASK,
    NODE_TASK_PREFIX,
    SafetyHoldError,
    TaskCommit,
    _require_run,
    _save_state_and_enqueue,
    _task_commit,
)
from co_scientist.orchestration.repository import events, tasks
from co_scientist.orchestration.repository.tasks import NewTask
from co_scientist.orchestration.run_events import make_emitter
from co_scientist.orchestration.safety_gate import apply_safety_gate
from co_scientist.platform.db import connect, runs, transaction
from co_scientist.platform.db.admission import claim_continuation
from co_scientist.platform.db.launch_control import LaunchPausedError
from co_scientist.platform.db.models import (
    TERMINAL_STATUSES,
    RunRow,
    RunStatus,
    ScientificTask,
    row_to_task,
)
from co_scientist.platform.llm.routing import (
    RunReadmission,
    prepare_readmission,
    recheck_readmission,
)

# Human categorical verdicts share agents' 1-10 rubric because the latest review
# score enters ranking/evolution prompts.
VERDICT_REVIEW_SCORES: dict[str, int] = {
    "support": 8,
    "revise": NEEDS_REVISION_SCORE,
    "oppose": NOT_VIABLE_SCORE,
}


def enqueue_bootstrap(
    run_id: str,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> ScientificTask:
    return tasks.enqueue_task(
        NewTask(
            run_id=run_id,
            task_type=BOOTSTRAP_TASK,
            inputs={},
            idempotency_key="engine:bootstrap:v1",
            priority=100,
            provenance={
                "behavior": "evidence-bounded-reconstruction",
                "scheduler": "durable-specialist-tasks-v1",
            },
            budget={"lease_seconds": 300},
        ),
        db_path=db_path,
        conn=conn,
    )


def _bootstrap_start_status(task: ScientificTask, run: RunRow, db_path: str | None) -> str:
    status = runs.mark_bootstrap_running(
        run.id,
        task.id,
        task.lease_owner,
        task.attempt,
        db_path=db_path,
    )
    if status is None:
        from co_scientist.orchestration.task_worker.outcomes import LeaseLostError

        raise LeaseLostError(f"bootstrap task {task.id} lost its lease before run start")
    return status


async def _screen_bootstrap_intake(
    run: RunRow,
    emit: Any,
    db_path: str | None,
    task: ScientificTask | None = None,
) -> dict[str, Any] | None:
    screen_with_escalation = engine_tasks_runtime.active().screen
    decision = await screen_with_escalation(
        run.id,
        ScreenSubject("intake", run.research_goal, screen_intake(run.research_goal)),
        provider=run.provider,
        db_path=db_path,
    )
    lease_guard = (task.id, task.lease_owner, task.attempt) if task is not None else None
    async for _ in apply_safety_gate(
        run.id,
        decision,
        emit,
        db_path=db_path,
        lease_guard=lease_guard,
        task=task,
    ):
        pass
    if decision.decision == "hold":
        raise SafetyHoldError(f"intake held for review: {decision.reason}")
    if decision.decision == "block":
        return {"run_id": run.id, "status": "withheld", "terminal": True}
    return None


def reopen_for_pending_scientist_input(
    run_id: str, *, db_path: str | None = None
) -> ScientificTask | None:
    """Finalize checks unread steering to recover contributions posted after
    the last orchestrator boundary without reopening again after
    acknowledgement.
    """
    pending = messages.get_pending_steering(run_id, db_path=db_path)
    if not pending:
        return None
    try:
        return enqueue_scientist_continuation(run_id, pending[0].id, db_path=db_path)
    except (ContinuationAdmissionError, LaunchPausedError) as exc:
        # A settled report stays completed; rejected late guidance remains pending.
        logging.getLogger(__name__).info(
            "Continuation admission denied for run %s: %s", run_id, exc
        )
        return None


def resume_pending_scientist_inputs() -> None:
    """Recover guidance admitted before a pause but deferred by finalization."""
    with connect() as conn:
        pending = conn.execute(
            "SELECT r.id AS run_id, MIN(m.id) AS input_id FROM runs r "
            "JOIN messages m ON m.run_id=r.id "
            "LEFT JOIN scientific_tasks t ON t.run_id=r.id "
            "AND t.idempotency_key='engine:scientist-continuation:' || m.id "
            "WHERE r.status='completed' AND m.kind='steering' AND m.applied=0 "
            "AND t.id IS NULL GROUP BY r.id ORDER BY MIN(m.id)"
        ).fetchall()
    for row in pending:
        try:
            enqueue_scientist_continuation(row["run_id"], row["input_id"])
        except LaunchPausedError:
            return
        except ContinuationAdmissionError as exc:
            logging.getLogger(__name__).info(
                "Continuation admission denied for run %s: %s", row["run_id"], exc
            )


def prepare_scientist_continuation(
    run_id: str, *, db_path: str | None = None
) -> RunReadmission | None:
    """Prepare provider admission without holding SQLite's writer lock."""
    before = runs.get_run(run_id, db_path=db_path)
    if before is None:
        return None
    try:
        return prepare_readmission(run_id, before.research_goal, before.config, db_path=db_path)
    except ProviderAdmissionError as error:
        raise ContinuationAdmissionError(str(error)) from error


def enqueue_scientist_continuation(
    run_id: str,
    input_id: int,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
    admission: RunReadmission | None = None,
) -> ScientificTask | None:
    key = f"engine:scientist-continuation:{input_id}"
    if conn is None:
        with connect(db_path) as reader:
            existing = reader.execute(
                "SELECT * FROM scientific_tasks WHERE run_id=? AND idempotency_key=?", (run_id, key)
            ).fetchone()
            if existing:
                return row_to_task(existing)
        admission = prepare_scientist_continuation(run_id, db_path=db_path)
    with nullcontext(conn) if conn is not None else transaction(db_path) as conn:
        run = runs.get_run(run_id, conn=conn)
        if run is None or run.provider != "engine":
            return None
        existing = conn.execute(
            "SELECT * FROM scientific_tasks WHERE run_id=? AND idempotency_key=?", (run_id, key)
        ).fetchone()
        if existing:
            return row_to_task(existing)
        if run.status != RunStatus.COMPLETED.value:
            return None
        from co_scientist.platform.db.launch_control import require_unpaused

        require_unpaused(conn=conn)
        checkpoint = conn.execute(
            "SELECT seq FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
        ).fetchone()
        if checkpoint is None:
            return None
        if not runs.has_run_capacity_in_transaction(
            conn, run_id, run.client_id, settings.max_concurrent_runs
        ):
            raise ContinuationAdmissionError("concurrent run limit reached", capacity=True)
        byok = conn.execute("SELECT 1 FROM run_credentials WHERE run_id=?", (run_id,)).fetchone()
        try:
            recheck_readmission(conn, run_id, admission)
            claim_continuation(
                conn, run_id, input_id, run.client_id, free=run.llm_backend == "real" and not byok
            )
        except ProviderAdmissionError as exc:
            raise ContinuationAdmissionError(str(exc)) from exc
        runs.update_run_status(run_id, RunStatus.QUEUED, conn=conn)
        events.append_event_deferred_log(
            run_id,
            "lifecycle",
            {"event": "reopened_for_scientist_input", "input_id": input_id},
            conn,
        )
        return tasks.enqueue_task(
            NewTask(
                run_id=run_id,
                task_type=f"{NODE_TASK_PREFIX}orchestrator",
                inputs={"checkpoint_seq": int(checkpoint["seq"])},
                idempotency_key=key,
                priority=100,
                provenance={"behavior": "scientist-directed-continuation", "input_id": input_id},
                budget={"lease_seconds": 300},
            ),
            conn=conn,
        )


# Checkpointed author enrichments preserve attribution without adding it to
# model input.
SCIENTIST_AUTHOR_MARK = "scientist_author"

# Unscreened placeholders must not become non-null state safety status, which
# would suppress the engine's check.
_UNSCREENED_SAFETY_STATUS = "pending"


def _admitted_safety_status(row: dict[str, Any]) -> str | None:
    status = str(row.get("safety_status") or "")
    if not status or status == _UNSCREENED_SAFETY_STATUS:
        return None
    return status


def _admitted_hypothesis(row: dict[str, Any]) -> Any:
    """Carry screened provenance into state, but never an unscreened
    placeholder that would suppress the engine's safety check.
    """
    from co_scientist.domains.research_state.models import Hypothesis, HypothesisOrigin

    hypothesis = Hypothesis(
        id=str(row["id"]),
        text=str(row.get("statement") or row.get("title") or ""),
        origin=HypothesisOrigin.SCIENTIST_MANUAL,
        explanation=str(row.get("title") or "") or None,
    )
    hypothesis.elo_rating = int(row.get("elo_rating") or INITIAL_ELO)
    hypothesis.safety_status = _admitted_safety_status(row)
    hypothesis.enrichments[SCIENTIST_AUTHOR_MARK] = str(row.get("author") or "")
    return hypothesis


def _merge_scientist_hypotheses(
    hypotheses: list[Any],
    by_id: dict[str, Any],
    run_id: str,
    db_path: str | None,
) -> None:
    for row in store_hypotheses.list_hypotheses(run_id, db_path=db_path):
        if row.get("created_by_agent") != "scientist_manual":
            continue
        hypothesis_id = str(row["id"])
        if hypothesis_id in by_id:
            continue
        hypothesis = _admitted_hypothesis(row)
        hypotheses.append(hypothesis)
        by_id[hypothesis_id] = hypothesis


def _row_verdict(row: dict[str, Any]) -> str:
    """Unknown verdicts default to neutral revise rather than endorsement or
    condemnation.
    """
    verdict = str(row.get("verdict") or "").strip().lower()
    return verdict if verdict in VERDICT_REVIEW_SCORES else "revise"


def _scientist_hypothesis_review(row: dict[str, Any]) -> Any:
    """Checkpointed feedback carries author, verdict and source row identity
    so drain restores scientist attribution.
    """
    from co_scientist.domains.research_state.models import SCIENTIST_REVIEWER, HypothesisReview

    verdict = _row_verdict(row)
    score = VERDICT_REVIEW_SCORES[verdict]
    critique = str(row.get("critique") or "")
    summary = str(row.get("summary") or "")
    return HypothesisReview(
        review_summary=f"{_review_marker(row)} {summary}",
        scores={"scientist_assessment": score},
        safety_ethical_concerns="",
        detailed_feedback={
            "scientist_critique": critique,
            "scientist_author": str(row.get("author") or ""),
            "scientist_verdict": verdict,
            "scientist_review_id": str(row["id"]),
        },
        constructive_feedback=critique,
        overall_score=float(score),
        # Scientist verdicts influence disposition but do not satisfy the
        # outstanding peer-review requirement.
        reviewer=SCIENTIST_REVIEWER,
    )


def _review_marker(row: dict[str, Any]) -> str:
    return f"[scientist-review:{row['id']}]"


def _merge_scientist_reviews(
    by_id: dict[str, Any],
    run_id: str,
    db_path: str | None,
) -> None:
    for row in records.list_reviews(run_id, db_path=db_path):
        if row.get("reviewer_agent") != "scientist":
            continue
        hypothesis = by_id.get(str(row.get("hypothesis_id")))
        if hypothesis is None:
            continue
        marker = _review_marker(row)
        if any(marker in review.review_summary for review in hypothesis.reviews):
            continue
        hypothesis.reviews.append(_scientist_hypothesis_review(row))


def _merge_scientist_inputs(
    state: dict[str, Any],
    run_id: str,
    db_path: str | None,
    *,
    admit_hypotheses: bool = True,
) -> None:
    """New hypotheses enter only at safe boundaries, never inside ranking or
    between fan-out items and their aggregate.
    """
    hypotheses = list(state.get("hypotheses") or [])
    by_id = {hypothesis.id: hypothesis for hypothesis in hypotheses}
    if admit_hypotheses:
        _merge_scientist_hypotheses(hypotheses, by_id, run_id, db_path)
    _merge_scientist_reviews(by_id, run_id, db_path)
    state["hypotheses"] = hypotheses
    _refresh_dispositions(hypotheses, state)


def _refresh_dispositions(hypotheses: list[Any], state: dict[str, Any]) -> None:
    """Scientist verdicts affect the next node without waiting for another
    paid review pass.
    """
    from co_scientist.science.reflection.review_gate import (
        refresh_review_dispositions,
    )

    refresh_review_dispositions(hypotheses, state.get("criteria"))


async def _prepare_bootstrap_state(
    task: ScientificTask, run: RunRow, db_path: str | None
) -> tuple[dict[str, Any], TaskCommit]:
    """Bootstrap incorporates guidance but leaves steering pending for the
    first orchestrator decision; pause is fenced by the commit
    transaction.
    """
    generator, opts = engine_tasks_runtime.active().generator_and_opts(task, db_path)
    state = await generator.prepare_task_state(
        run.research_goal,
        opts=opts,
        run_id=run.id,
    )
    commit = _task_commit(task, 0, db_path, opts, consume_steering=False)
    refreshed = runs.get_run(run.id, db_path=db_path)
    if refreshed is None or refreshed.status == RunStatus.CANCELLED.value:
        raise RuntimeError("run cancelled during bootstrap")
    # Pause snapshots are advisory; the commit transaction resolves a concurrent
    # resume before choosing the successor.
    return state, commit


async def execute_bootstrap(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
    run = _require_run(task, db_path)
    emit = make_emitter(run.id, db_path=db_path)
    withheld = await _screen_bootstrap_intake(run, emit, db_path, task=task)
    if withheld is not None:
        return withheld
    run = _require_run(task, db_path)  # the gate may have redacted the goal
    bootstrap_status = _bootstrap_start_status(task, run, db_path)
    if bootstrap_status in {status.value for status in TERMINAL_STATUSES}:
        return {"run_id": run.id, "status": bootstrap_status, "terminal": True}
    # Persist the resolved backend before generator construction reads run
    # provenance.
    sync_engine_llm_backend(run.id, resolved_run_config(run.config), db_path)
    state, commit = await _prepare_bootstrap_state(task, run, db_path)
    checkpoint_seq, successor_id = _save_state_and_enqueue(
        commit, state, "supervisor", pause_if_requested=True
    )
    if successor_id is None:
        return {"checkpoint_seq": checkpoint_seq, "status": "paused"}
    await emit(
        "scientific_task",
        {
            "task": "bootstrap",
            "status": "completed",
            "checkpoint_seq": checkpoint_seq,
        },
    )
    return {
        "checkpoint_seq": checkpoint_seq,
        "successor_task_id": successor_id,
    }
