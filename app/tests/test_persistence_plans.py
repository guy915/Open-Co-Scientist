from __future__ import annotations

import pathlib
from typing import Any

import pytest
from co_scientist.domains.report import repository as reports
from co_scientist.platform import db
from co_scientist.platform.db import supervisor_plan as plans

from tests._drain_helpers import (
    _final_state_with_features,
    _persist_and_finalize,
)
from tests._store_helpers import seed_checkpoint, seed_run


def _allocations(run_id: str, db_path: str) -> list[dict[str, Any]]:
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM supervisor_allocations WHERE run_id=? ORDER BY seq ASC",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_reports_round_trip_full_markdown_through_database(
    isolated_db: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    run = seed_run("report rt", profile="default", provider="mock", db_path=isolated_db)
    saved = reports.save_report(run.id, {"k": "v"}, "# Hello\nbody", db_path=isolated_db)
    md = reports.read_report_markdown(run.id, db_path=isolated_db)
    assert md == "# Hello\nbody"
    rep = reports.get_latest_report(run.id, db_path=isolated_db)
    assert rep and rep["payload"] == {"k": "v"}
    assert rep["id"] == saved["id"]
    assert rep["markdown_text"] == "# Hello\nbody"
    assert rep["markdown_path"] == ""
    assert not (tmp_path / "reports").exists()


def _plan_final_state() -> dict[str, object]:
    state = _final_state_with_features()
    state["supervisor_guidance"] = {
        "research_goal_analysis": {"key_areas": ["oncology"]},
        "workflow_plan": {"iterations": 2},
        "config_synthesis": {"tone": "concise"},
        "performance_assessment": {},
        "adjustment_recommendations": [],
        "output_preparation": {},
    }
    state["orchestrator_state"] = {"previous_top_elo": 1320, "pool_size": 4}
    state["supervisor_decision_provenance"] = "model"
    state["termination_reason"] = "satisfied_completion"
    state["task_history"] = [
        {
            "task_type": "generate",
            "status": "queued",
            "reason": "Supervisor selected generate from live state: "
            "0 hypotheses, 0 reviewed, 0 committed matches, "
            "iteration 1.",
            "iteration": 1,
            "termination_reason": None,
            "priority": 50,
            "planner_reason": "Seed the initial hypothesis pool.",
        },
        {
            "task_type": "rank",
            "status": "queued",
            "reason": "Supervisor selected rank from live state: "
            "2 hypotheses, 2 reviewed, 0 committed matches, "
            "iteration 1.",
            "iteration": 1,
            "termination_reason": None,
            "priority": 40,
            "planner_reason": "Establish an initial ordering.",
        },
        {
            "task_type": "terminate",
            "status": "completed",
            "reason": "Supervisor selected terminate from live state: "
            "2 hypotheses, 2 reviewed, 1 committed matches, "
            "iteration 1.",
            "iteration": 1,
            "termination_reason": "satisfied_completion",
            "priority": 0,
            "planner_reason": "Budget exhausted with a stable ranking.",
        },
    ]
    return state


def test_finalize_persists_supervisor_plan_and_allocations(
    isolated_db: str,
) -> None:
    run = seed_run("sp e2e goal", provider="mock")
    final_state = _plan_final_state()

    _persist_and_finalize(run, final_state, isolated_db)

    plan = plans.get_supervisor_plan(run.id, db_path=isolated_db)
    assert plan is not None
    assert plan["plan"]["workflow_plan"] == {"iterations": 2}
    assert plan["decision_provenance"] == "model"
    assert plan["termination_reason"] == "satisfied_completion"
    assert plan["orchestrator_state"] == {
        "previous_top_elo": 1320,
        "pool_size": 4,
    }

    allocations = _allocations(run.id, isolated_db)
    assert [a["task_type"] for a in allocations] == [
        "generate",
        "rank",
        "terminate",
    ]
    assert allocations[-1]["termination_reason"] == "satisfied_completion"
    assert "2 hypotheses" in allocations[-1]["reason"]


# Failure/cancellation can skip finalize, so checkpoint commits must persist
# supervisor provenance too.


def _checkpoint_state(
    *,
    task_history: list[dict[str, Any]] | None = None,
    guidance: dict[str, Any] | None = None,
    orchestrator_state: dict[str, Any] | None = None,
    decision_provenance: str | None = None,
    termination_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "provider": "engine",
        "version": 1,
        "last_event_seq": 0,
        "state": {
            "task_history": task_history or [],
            "supervisor_guidance": guidance or {},
            "orchestrator_state": orchestrator_state or {},
            "supervisor_decision_provenance": decision_provenance,
            "termination_reason": termination_reason,
        },
    }


def _task(task_type: str, iteration: int = 1, **overrides: Any) -> dict[str, Any]:
    return {
        "task_type": task_type,
        "status": "queued",
        "reason": f"Supervisor selected {task_type} from live state.",
        "iteration": iteration,
        **overrides,
    }


_GUIDANCE = {"workflow_plan": {"iterations": 2}}


def test_shorter_later_checkpoint_never_shrinks_the_ledger(
    isolated_db: str,
) -> None:
    # Older out-of-order checkpoints must not erase a more complete stored
    # ledger.
    run = seed_run("goal", provider="mock")
    seed_checkpoint(
        run.id,
        _checkpoint_state(
            task_history=[_task("generate"), _task("rank")],
            guidance=_GUIDANCE,
        ),
        stage="engine_task:t1",
        last_event_seq=1,
        db_path=isolated_db,
    )

    seed_checkpoint(
        run.id,
        _checkpoint_state(task_history=[_task("generate")], guidance=_GUIDANCE),
        stage="engine_task:stale",
        last_event_seq=1,
        db_path=isolated_db,
    )

    allocations = _allocations(run.id, isolated_db)
    assert [a["task_type"] for a in allocations] == ["generate", "rank"]
