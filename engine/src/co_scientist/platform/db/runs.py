from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from co_scientist.core.prompt_layout import retire_run_prompt_context
from co_scientist.platform.db import connect, current_time, transaction, use_conn
from co_scientist.platform.db.admission import capacity_available
from co_scientist.platform.db.llm_routes import release_forecast
from co_scientist.platform.db.logs import count_logs_for_run, delete_logs_for_run
from co_scientist.platform.db.models import (
    _ACTIVE_RUN_STATUSES,
    DEMO_CLIENT_ID,
    TERMINAL_STATUSES,
    RunRow,
    RunStatus,
    row_to_run,
)

logger = logging.getLogger(__name__)


def _bootstrap_lease_matches(row: sqlite3.Row, worker_id: str | None, attempt: int) -> bool:
    return all(
        (
            row["task_status"] == "leased",
            row["lease_owner"] == worker_id,
            row["attempt"] == attempt,
            row["task_type"] == "engine.bootstrap",
            row["lease_expires_at"] is not None,
            float(row["lease_expires_at"] or 0) > current_time(),
        )
    )


def bootstrap_task_lease_matches(
    conn: sqlite3.Connection,
    run_id: str,
    task_id: str,
    worker_id: str | None,
    attempt: int,
) -> bool:
    row = conn.execute(
        "SELECT status AS task_status, lease_owner, attempt, task_type, "
        "lease_expires_at FROM scientific_tasks WHERE id=? AND run_id=?",
        (task_id, run_id),
    ).fetchone()
    return row is not None and _bootstrap_lease_matches(row, worker_id, attempt)


def _allowed_terminal_bootstrap_status(
    status: str,
    row: sqlite3.Row,
    worker_id: str | None,
    attempt: int,
) -> str | None:
    if status == RunStatus.PAUSED.value and not _bootstrap_lease_matches(row, worker_id, attempt):
        return None
    terminal_or_paused = {
        RunStatus.COMPLETED.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
        RunStatus.BLOCKED.value,
        RunStatus.PAUSED.value,
    }
    return status if status in terminal_or_paused else None


def _advance_bootstrap_status(conn: sqlite3.Connection, run_id: str, status: str) -> bool:
    if status not in {
        RunStatus.RUNNING.value,
        RunStatus.DRAFT.value,
        RunStatus.QUEUED.value,
    }:
        return False
    if status != RunStatus.RUNNING.value:
        now = current_time()
        conn.execute(
            "UPDATE runs SET status=?, error=NULL, updated_at=?, "
            "completed_at=NULL WHERE id=? AND status=?",
            (RunStatus.RUNNING.value, now, run_id, status),
        )
    return True


def mark_bootstrap_running(
    run_id: str,
    task_id: str,
    worker_id: str | None,
    attempt: int,
    db_path: str | None = None,
) -> str | None:
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT runs.status AS run_status, task.status AS task_status, "
            "task.lease_owner, task.attempt, task.task_type, "
            "task.lease_expires_at "
            "FROM runs LEFT JOIN scientific_tasks AS task "
            "ON task.id=? AND task.run_id=runs.id WHERE runs.id=?",
            (task_id, run_id),
        ).fetchone()
        if row is None:
            return None
        status = str(row["run_status"])
        terminal_status = _allowed_terminal_bootstrap_status(status, row, worker_id, attempt)
        if terminal_status is not None:
            return terminal_status
        if not _bootstrap_lease_matches(row, worker_id, attempt):
            return None
        if not _advance_bootstrap_status(conn, run_id, status):
            return None
    return RunStatus.RUNNING.value


# Cascade row accounting spans direct run children and FK descendants so
# deletion can be verified.
_RUN_ID_TABLES: tuple[str, ...] = (
    "run_credentials",
    "run_events",
    "scientific_tasks",
    "hypotheses",
    "evidence",
    "citations",
    "reviews",
    "matches",
    "safety_decisions",
    "reports",
    "messages",
    "checkpoints",
    "run_metrics",
    "claim_evidence",
    "knowledge_facts",
    "proximity_edges",
)

# Hypothesis state has no run_id; account for its transitive cascade through the
# hypothesis join.
_HYPOTHESIS_SCOPED_TABLES: tuple[str, ...] = ("hypothesis_state",)


def count_run_rows(run_id: str, *, db_path: str | None = None) -> dict[str, int]:
    with connect(db_path) as conn:
        counts = {
            "runs": conn.execute("SELECT COUNT(*) FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        }
        for table in _RUN_ID_TABLES:
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        for table in _HYPOTHESIS_SCOPED_TABLES:
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE hypothesis_id IN "
                "(SELECT id FROM hypotheses WHERE run_id=?)",
                (run_id,),
            ).fetchone()[0]
        counts["staged_documents"] = conn.execute(
            "SELECT COUNT(*) FROM staged_documents WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        counts["app_logs"] = count_logs_for_run(run_id, conn=conn)
    return counts


def delete_run(run_id: str, *, draft_before: float | None = None) -> dict[str, int]:
    before = count_run_rows(run_id)
    with transaction() if draft_before is not None else connect() as conn:
        if (
            draft_before is not None
            and not conn.execute(
                "SELECT 1 FROM runs r WHERE id=? AND status='draft' AND updated_at<? "
                "AND NOT EXISTS (SELECT 1 FROM scientific_tasks t WHERE t.run_id=r.id "
                "AND t.status IN ('queued','leased'))",
                (run_id, draft_before),
            ).fetchone()
        ):
            return {}
        conn.execute(
            "UPDATE staged_documents SET run_id=NULL WHERE run_id=?",
            (run_id,),
        )
        delete_logs_for_run(run_id, conn=conn)
        conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
    retire_run_prompt_context(run_id)
    return before


def run_used_offline(run: RunRow) -> bool:
    """Historical backend comes from the run row, not current process
    configuration.
    """
    return run.llm_backend == "offline"


def run_offline_backed(run_id: str, *, db_path: str | None = None) -> bool:
    run = get_run(run_id, db_path=db_path)
    return run is not None and run_used_offline(run)


def set_run_llm_backend(run_id: str, llm_backend: str, db_path: str | None = None) -> None:
    """Persist resolved overrides before execution because later publication
    and badging readers reload the run row.
    """
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET llm_backend = ? WHERE id = ?",
            (llm_backend, run_id),
        )


def redact_run_goal(
    run_id: str,
    goal: str,
    title: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Redact title and clear restatement with the goal: both are derived
    text that could otherwise disclose the same sensitive span.
    """
    with use_conn(conn, db_path) as active:
        active.execute(
            "UPDATE runs SET research_goal = ?, title = ?, goal_restatement = NULL WHERE id = ?",
            (goal, title, run_id),
        )


def get_run(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> RunRow | None:
    with use_conn(conn, db_path) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row_to_run(row) if row else None


def run_exists(run_id: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row is not None


def update_run_status(
    run_id: str,
    status: RunStatus,
    error: str | None = None,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    now = current_time()
    completed_at = now if status in TERMINAL_STATUSES else None
    with use_conn(conn, db_path) as active:
        active.execute(
            "UPDATE runs SET status=?, error=?, updated_at=?, completed_at=? WHERE id=?",
            (status.value, error, now, completed_at, run_id),
        )
        if status in TERMINAL_STATUSES:
            release_forecast(active, run_id)


def update_run_status_if_current(
    conn: sqlite3.Connection,
    run_id: str,
    status: RunStatus,
    expected_statuses: tuple[RunStatus, ...],
    error: str | None = None,
) -> bool:
    if not expected_statuses:
        return False
    now = current_time()
    completed_at = now if status in TERMINAL_STATUSES else None
    placeholders = ",".join("?" for _ in expected_statuses)
    changed = conn.execute(
        "UPDATE runs SET status=?, error=?, updated_at=?, completed_at=? "
        f"WHERE id=? AND status IN ({placeholders})",
        (
            status.value,
            error,
            now,
            completed_at,
            run_id,
            *(item.value for item in expected_statuses),
        ),
    ).rowcount
    if changed and status in TERMINAL_STATUSES:
        release_forecast(conn, run_id)
    return bool(changed)


def summary_counts(run_id: str, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    tables = {
        "events": "run_events",
        "hypotheses": "hypotheses",
        "evidence": "evidence",
        "matches": "matches",
        "reviews": "reviews",
    }
    with use_conn(conn, None) as conn:
        return {
            field: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            for field, table in tables.items()
        }


def set_run_title(run_id: str, title: str, db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE runs SET title = ? WHERE id = ?", (title, run_id))


def set_run_goal_restatement(run_id: str, restatement: str, db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET goal_restatement = ? WHERE id = ?",
            (restatement, run_id),
        )


@dataclass(frozen=True)
class RunCreateOptions:
    client_id: str = ""
    title: str | None = None
    llm_backend: str | None = None
    db_path: str | None = None
    conn: sqlite3.Connection | None = None
    log_created: bool | None = None


def log_run_created(run: RunRow) -> None:
    logger.info(
        "created run %s run_mode=%s provider=%s llm_backend=%s client_id=%s",
        run.id,
        run.profile,
        run.provider,
        run.llm_backend,
        run.client_id,
    )


def create_run(
    research_goal: str,
    profile: str,
    provider: str,
    config: dict[str, Any],
    options: RunCreateOptions | None = None,
) -> RunRow:
    """Caller-owned transactions defer logging until commit so rolled-back
    creation cannot appear in the log.
    """
    opts = options or RunCreateOptions()
    if opts.client_id == DEMO_CLIENT_ID:
        raise ValueError("the demo identity is reserved for seeded examples")
    now = current_time()
    backend = opts.llm_backend or "real"
    run = RunRow(
        id=str(uuid.uuid4()),
        research_goal=research_goal,
        title=opts.title,
        profile=profile,
        status=RunStatus.DRAFT.value,
        provider=provider,
        config=config,
        client_id=opts.client_id,
        created_at=now,
        updated_at=now,
        completed_at=None,
        error=None,
        llm_backend=backend,
    )
    with use_conn(opts.conn, opts.db_path) as active:
        active.execute(
            "INSERT INTO runs (id, research_goal, title, profile, status, "
            "provider, config_json, client_id, created_at, updated_at, "
            "llm_backend) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.id,
                run.research_goal,
                run.title,
                run.profile,
                run.status,
                run.provider,
                json.dumps(run.config),
                run.client_id,
                now,
                now,
                run.llm_backend,
            ),
        )
    should_log = opts.conn is None if opts.log_created is None else opts.log_created
    if should_log:
        log_run_created(run)
    return run


def _count_other_active_runs(conn: sqlite3.Connection, run_id: str, client_id: str) -> int:
    """The concurrency allowance spans every tier for one identity; counting
    per tier would multiply the advertised limit.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM runs WHERE client_id=? AND status IN (?,?,?) AND id!=?",
            (client_id, *_ACTIVE_RUN_STATUSES, run_id),
        ).fetchone()[0]
    )


def _queue_run_if_startable(
    conn: sqlite3.Connection, run_id: str, now: float, expected_status: str
) -> int:
    if expected_status not in {
        RunStatus.DRAFT.value,
        RunStatus.FAILED.value,
        RunStatus.BLOCKED.value,
        RunStatus.CANCELLED.value,
    }:
        return 0
    return conn.execute(
        "UPDATE runs SET status=?, updated_at=?, completed_at=NULL, "
        "error=NULL WHERE id=? AND status=?",
        (
            RunStatus.QUEUED.value,
            now,
            run_id,
            expected_status,
        ),
    ).rowcount


def reserve_run_capacity_in_transaction(
    conn: sqlite3.Connection,
    run_id: str,
    client_id: str,
    limit: int,
    expected_status: str,
) -> bool:
    if not has_run_capacity_in_transaction(conn, run_id, client_id, limit):
        return False
    changed = _queue_run_if_startable(conn, run_id, current_time(), expected_status)
    return bool(changed)


def has_run_capacity_in_transaction(
    conn: sqlite3.Connection, run_id: str, client_id: str, limit: int
) -> bool:
    return _count_other_active_runs(conn, run_id, client_id) < limit and capacity_available(
        conn, run_id, limit
    )


_TOP_HYPOTHESES_CAP = 3

# Curated seeds emit these stages; durable live progress instead comes from
# leased tasks, and cross-cutting events are excluded.
_STAGE_EVENT_TYPES: tuple[str, ...] = (
    "supervisor.plan",
    "literature_review",
    "generate",
    "reflection",
    "proximity",
    "ranking",
    "evolve",
    "meta_review",
    "deep_verification",
    "research_overview",
)


def _top_hypotheses_by_run(conn: sqlite3.Connection, run_ids: list[str]) -> dict[str, list[str]]:
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        "SELECT run_id, title FROM ("
        " SELECT h.run_id AS run_id, h.title AS title, ROW_NUMBER() OVER ("
        "  PARTITION BY h.run_id"
        "  ORDER BY s.elo_rating DESC, h.created_at ASC, h.id"
        " ) AS rn"
        " FROM hypotheses h"
        " JOIN hypothesis_state s ON s.hypothesis_id = h.id"
        f" WHERE h.run_id IN ({placeholders})"
        ") WHERE rn <= ? ORDER BY run_id, rn",
        (*run_ids, _TOP_HYPOTHESES_CAP),
    ).fetchall()
    by_run: dict[str, list[str]] = {}
    for row in rows:
        by_run.setdefault(row["run_id"], []).append(row["title"])
    return by_run


def _latest_stage_by_run(conn: sqlite3.Connection, run_ids: list[str]) -> dict[str, str]:
    if not run_ids:
        return {}
    # One indexed newest-first probe per run; a window over every matching
    # event grows with each run's whole history.
    stage_placeholders = ",".join("?" for _ in _STAGE_EVENT_TYPES)
    query = (
        "SELECT type FROM run_events"
        f" WHERE run_id = ? AND type IN ({stage_placeholders})"
        " ORDER BY seq DESC LIMIT 1"
    )
    latest: dict[str, str] = {}
    for run_id in run_ids:
        row = conn.execute(query, (run_id, *_STAGE_EVENT_TYPES)).fetchone()
        if row is not None:
            latest[run_id] = row["type"]
    return latest


def list_expired_terminal_runs(cutoff: float, db_path: str | None = None) -> list[RunRow]:
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE status IN "
            f"({placeholders}) AND COALESCE(completed_at, updated_at) < ? "
            "ORDER BY COALESCE(completed_at, updated_at) ASC",
            (*(status.value for status in TERMINAL_STATUSES), cutoff),
        ).fetchall()
    return [row_to_run(row) for row in rows]


def list_expired_draft_runs(cutoff: float, db_path: str | None = None) -> list[RunRow]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs r WHERE status='draft' AND updated_at<? "
            "AND NOT EXISTS (SELECT 1 FROM scientific_tasks t WHERE t.run_id=r.id "
            "AND t.status IN ('queued','leased')) ORDER BY updated_at ASC",
            (cutoff,),
        ).fetchall()
    return [row_to_run(row) for row in rows]


def list_runs(client_id: str = "", limit: int = 100, db_path: str | None = None) -> list[RunRow]:
    with connect(db_path) as conn:
        rows = conn.execute(
            # A correlated MAX touches only listed runs; a grouped join
            # aggregates every hypothesis in the store.
            "SELECT r.*, ("
            " SELECT MAX(s.elo_rating) FROM hypotheses h "
            " JOIN hypothesis_state s ON s.hypothesis_id = h.id "
            " WHERE h.run_id = r.id) AS top_elo "
            "FROM runs r "
            "WHERE r.client_id = ? "
            "ORDER BY r.created_at DESC LIMIT ?",
            (client_id, limit),
        ).fetchall()
        runs = [row_to_run(r) for r in rows]
        run_ids = [run.id for run in runs]
        top_hypotheses = _top_hypotheses_by_run(conn, run_ids)
        latest_stage = _latest_stage_by_run(conn, run_ids)
    for run in runs:
        # Listed runs without hypotheses use []; single reads retain the
        # unenriched None default.
        run.top_hypotheses = top_hypotheses.get(run.id, [])
        run.latest_stage = latest_stage.get(run.id)
    return runs


_PROGRESS_QUERY = (
    "SELECT COUNT(*) AS total,"
    " COALESCE(SUM(status IN ('completed','failed','cancelled')), 0)"
    " AS completed,"
    " COALESCE(SUM(status='queued'), 0) AS queued,"
    " COALESCE(MAX(substr(task_type,1,7)='engine.'), 0) AS dynamic_plan,"
    " (SELECT task_type FROM scientific_tasks WHERE run_id=? AND"
    "  status IN ('leased','running')"
    "  ORDER BY created_at ASC LIMIT 1) AS active_task"
    " FROM scientific_tasks WHERE run_id=?"
)


def task_progress(
    run_id: str,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    with use_conn(conn, db_path) as active:
        row = active.execute(
            _PROGRESS_QUERY,
            (run_id, run_id),
        ).fetchone()
    total = int(row["total"])
    completed = int(row["completed"])
    # Model-expanded plans have no honest denominator and must not report
    # determinate fractional progress.
    determinate = total > 0 and not row["dynamic_plan"]
    return {
        "determinate": determinate,
        "completed_tasks": completed,
        "total_tasks": total,
        "fraction": completed / total if determinate else None,
        "active_task": row["active_task"],
        "queued_tasks": int(row["queued"]),
    }


def recent_events(
    run_id: str,
    limit: int,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Bound the decoded Q&A context tail rather than loading the run's
    entire event history.
    """
    with use_conn(conn, None) as active:
        rows = active.execute(
            "SELECT seq, type, payload_json, created_at FROM run_events "
            "WHERE run_id=? ORDER BY seq DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
    return [
        {
            "seq": r["seq"],
            "type": r["type"],
            "payload": json.loads(r["payload_json"]),
            "created_at": r["created_at"],
        }
        for r in reversed(rows)
    ]


def run_execution_started_at(run_id: str, conn: sqlite3.Connection | None = None) -> float | None:
    """Draft creation is not compute start; elapsed execution begins with
    the first queued lifecycle event.
    """
    with use_conn(conn, None) as active:
        row = active.execute(
            "SELECT MIN(created_at) AS started FROM run_events WHERE run_id=? AND type='lifecycle'",
            (run_id,),
        ).fetchone()
    started = row["started"] if row is not None else None
    return float(started) if started is not None else None
