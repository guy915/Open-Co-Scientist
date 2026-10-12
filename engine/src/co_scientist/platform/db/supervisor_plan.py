from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from co_scientist.platform.db import current_time, use_conn


@dataclass(frozen=True)
class NewSupervisorPlan:
    run_id: str
    guidance: dict[str, Any]
    termination_reason: str | None
    decision_provenance: str | None
    orchestrator_state: dict[str, Any]


def save_supervisor_plan(
    plan: NewSupervisorPlan,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    now = current_time()
    with use_conn(conn, None) as active:
        active.execute(
            "INSERT INTO supervisor_plan (run_id, plan_json, "
            "orchestrator_state_json, decision_provenance, "
            "termination_reason, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "plan_json=excluded.plan_json, "
            "orchestrator_state_json=excluded.orchestrator_state_json, "
            "decision_provenance=excluded.decision_provenance, "
            "termination_reason=excluded.termination_reason, "
            "updated_at=excluded.updated_at",
            (
                plan.run_id,
                json.dumps(plan.guidance),
                json.dumps(plan.orchestrator_state),
                plan.decision_provenance,
                plan.termination_reason,
                now,
                now,
            ),
        )


def get_supervisor_plan(
    run_id: str,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with use_conn(conn, db_path) as active:
        row = active.execute("SELECT * FROM supervisor_plan WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["plan"] = json.loads(result.pop("plan_json"))
    result["orchestrator_state"] = json.loads(result.pop("orchestrator_state_json"))
    return result


def replace_supervisor_allocations(
    run_id: str,
    allocations: list[dict[str, Any]],
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Ledger sequence follows scheduling order; shared drain timestamps
    cannot recover that order.
    """
    with use_conn(conn, None) as active:
        active.execute("DELETE FROM supervisor_allocations WHERE run_id = ?", (run_id,))
        now = current_time()
        active.executemany(
            "INSERT INTO supervisor_allocations (run_id, seq, iteration, "
            "task_type, status, reason, planner_reason, priority, "
            "termination_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    seq,
                    int(entry.get("iteration", 0)),
                    entry["task_type"],
                    entry["status"],
                    entry["reason"],
                    entry.get("planner_reason"),
                    entry.get("priority"),
                    entry.get("termination_reason"),
                    now,
                )
                for seq, entry in enumerate(allocations)
            ],
        )


def _count_supervisor_allocations(run_id: str, conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM supervisor_allocations WHERE run_id=?",
        (run_id,),
    ).fetchone()
    return int(row[0])


def _has_supervisor_plan(run_id: str, conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM supervisor_plan WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
    return row is not None


def _sync_allocations_from_checkpoint(
    run_id: str,
    task_history: list[dict[str, Any]],
    conn: sqlite3.Connection,
) -> bool:
    """Task history is append-only within a lineage, so length guards stale
    commits and avoids rewrites when no decision was added.
    """
    if len(task_history) <= _count_supervisor_allocations(run_id, conn):
        return False
    replace_supervisor_allocations(run_id, task_history, conn=conn)
    return True


def _sync_plan_from_checkpoint(
    run_id: str,
    ledger_grew: bool,
    workflow_state: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    """Persist the first guidance before any allocation so a run dying in
    generation still leaves its research plan.
    """
    guidance = workflow_state.get("supervisor_guidance") or {}
    if not guidance:
        return
    if not ledger_grew and _has_supervisor_plan(run_id, conn):
        return
    save_supervisor_plan(
        NewSupervisorPlan(
            run_id=run_id,
            guidance=guidance,
            termination_reason=workflow_state.get("termination_reason"),
            decision_provenance=workflow_state.get("supervisor_decision_provenance"),
            orchestrator_state=workflow_state.get("orchestrator_state") or {},
        ),
        conn=conn,
    )


def sync_supervisor_ledger_from_checkpoint(
    run_id: str,
    checkpoint_state: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    """Checkpoint-atomic writes retain plans for non-finalizing runs;
    finalize remains authoritative for terminal scheduling provenance.
    """
    workflow_state = checkpoint_state.get("state") or {}
    task_history = workflow_state.get("task_history") or []
    ledger_grew = _sync_allocations_from_checkpoint(run_id, task_history, conn)
    _sync_plan_from_checkpoint(run_id, ledger_grew, workflow_state, conn)
