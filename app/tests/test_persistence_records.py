from __future__ import annotations

import io
import logging
import sqlite3
import time
from typing import Any, cast

import pytest
from co_scientist.domains.access import retention
from co_scientist.domains.documents import repository as documents
from co_scientist.domains.report import build as report_build
from co_scientist.domains.report import repository as reports
from co_scientist.domains.report.content import derive_knowledge_facts
from co_scientist.domains.research_state.claims.gate import ClaimEdge
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.records import NewClaimEvidence
from co_scientist.orchestration.engine_tasks import report_finalize
from co_scientist.orchestration.repository import tasks as store_tasks
from co_scientist.orchestration.repository.events import (
    ACTIVITY_OTHER,
    ACTIVITY_VALUES,
    activity_for_event,
)
from co_scientist.platform import db as _store_db
from co_scientist.platform.db import checkpoints as store_checkpoints
from co_scientist.platform.db import logs
from co_scientist.platform.db import runs as store
from co_scientist.platform.db.logs import NewLogRecord
from co_scientist.platform.db.models import RunStatus
from fastapi.testclient import TestClient

from tests._client import create_run as _create_run
from tests._client import drain as _drain
from tests._client import make_client, wait_for_status
from tests._client import make_client as _client
from tests._drain_helpers import emit_event
from tests._engine_tasks_helpers import _seed_checkpoint, _task_state
from tests._store_helpers import _add, seed_checkpoint, seed_run


def _edge(
    label: str,
    claim: str = "IL-6 increases inflammation via STAT3 signaling.",
    **over: Any,
) -> ClaimEdge:
    base: dict[str, Any] = {
        "hypothesis_id": "h1",
        "claim": claim,
        "label": label,
        "supporting": [],
        "contradicting": [],
    }
    base.update(over)
    return ClaimEdge.from_row(base)


def test_only_settled_edges_become_facts_with_their_own_evidence() -> None:
    facts = derive_knowledge_facts(
        [
            _edge(
                "supports",
                claim="TREM2 promotes microglial clearance.",
                supporting=[{"evidence_id": "ev-1"}],
                contradicting=[{"evidence_id": "ev-wrong"}],
            ),
            _edge("insufficient", claim="An insufficient claim."),
            _edge("supports", claim="  "),
            _edge(
                "contradicts",
                claim="A contradicts claim.",
                supporting=[{"evidence_id": "ev-wrong"}],
                contradicting=[{"evidence_id": "ev-2"}],
            ),
            _edge("supports", claim="No span."),
        ]
    )

    assert [
        (
            f["kind"],
            f["state"],
            f["statement"],
            f["hypothesis_id"],
            f["evidence_id"],
        )
        for f in facts
    ] == [
        (
            "fact",
            "supports",
            "TREM2 promotes microglial clearance.",
            "h1",
            "ev-1",
        ),
        ("contradiction", "contradicts", "A contradicts claim.", "h1", "ev-2"),
        ("fact", "supports", "No span.", "h1", None),
    ]
    assert "TREM2" in facts[0]["entities"]


def _make_run(client: TestClient, goal: str = "test goal") -> str:
    client.headers.update({"X-Client-ID": "test-client"})
    res = _create_run(client, goal, tier="express")
    assert res.status_code == 200
    return cast(str, res.json()["id"])


def test_steering_reopens_completed_engine_run(isolated_db: str) -> None:
    client = _client()
    client.headers.update({"X-Client-ID": "test-client"})
    run = seed_run("Completed research", client_id="test-client", db_path=isolated_db)
    state = {
        **_task_state(run.id),
        "research_goal": run.research_goal,
        "current_iteration": 1,
        "start_time": 1.0,
    }
    _seed_checkpoint(run.id, state, stage="engine_task:final", db_path=isolated_db)
    store.update_run_status(run.id, RunStatus.COMPLETED, db_path=isolated_db)

    response = client.post(
        f"/api/runs/{run.id}/messages",
        json={"content": "Test the mechanism in organoids next."},
    )

    assert response.status_code == 200
    assert response.json()["continuation_task_id"] is not None
    reopened = store.get_run(run.id, db_path=isolated_db)
    assert reopened is not None and reopened.status == "queued"
    tasks = store_tasks.list_tasks(run.id, db_path=isolated_db)
    assert tasks[-1].task_type == "engine.node.orchestrator"
    assert tasks[-1].priority == 100


def test_messages_are_queued_as_steering_and_listed_in_order(
    isolated_db: str,
) -> None:
    client = _client()
    run_id = _make_run(client)

    first = client.post(f"/api/runs/{run_id}/messages", json={"content": "steer A"})
    client.post(f"/api/runs/{run_id}/messages", json={"content": "steer B"})

    assert first.status_code == 200
    assert first.json()["kind"] == "steering"
    assert first.json()["status"] == "queued"
    assert first.json()["continuation_task_id"] is None
    res = client.get(f"/api/runs/{run_id}/messages")
    assert [m["content"] for m in res.json()["messages"]] == [
        "steer A",
        "steer B",
    ]


def test_steering_messages_applied_after_run(isolated_db: str) -> None:
    client = _client()
    run_id = _make_run(client, goal="test steering injection")

    client.post(
        f"/api/runs/{run_id}/messages",
        json={"content": "focus on apoptosis pathways", "kind": "steering"},
    )

    msgs_before = client.get(f"/api/runs/{run_id}/messages").json()["messages"]
    assert msgs_before[0]["applied"] is False

    client.post(f"/api/runs/{run_id}/start", json={})
    wait_for_status(client, run_id, "completed", timeout=20.0, interval=0.1)

    msgs_after = client.get(f"/api/runs/{run_id}/messages").json()["messages"]
    steering = [m for m in msgs_after if m["kind"] == "steering"]
    assert len(steering) == 1
    assert steering[0]["applied"] is True


def _backdate_completion(db_path: str, run_id: str, seconds_ago: float) -> None:
    # Write timestamps directly so age-based sweeps are deterministic without
    # real-time waits.
    backdated = time.time() - seconds_ago
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET completed_at=?, updated_at=? WHERE id=?",
            (backdated, backdated, run_id),
        )


def test_zero_disables_the_run_sweep(isolated_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCIENTIST_RUN_RETENTION_DAYS", "0")
    client = make_client()
    created = _create_run(
        client,
        "Old completed goal",
        headers={"X-Client-ID": "retention-tester"},
    )
    run_id = created.json()["id"]
    store.update_run_status(run_id, RunStatus.COMPLETED)

    deleted = retention.sweep_expired_runs(now=time.time() + 10_000 * 86_400)

    assert deleted == []
    assert store.run_exists(run_id)


def test_sweep_deletes_only_terminal_runs_past_the_window(
    isolated_db: str,
) -> None:
    client = make_client()
    old_completed = _create_run(
        client,
        "Old completed goal",
        headers={"X-Client-ID": "retention-tester"},
    ).json()["id"]
    store.update_run_status(old_completed, RunStatus.COMPLETED)
    _backdate_completion(isolated_db, old_completed, 120 * 86_400)

    old_running = _create_run(
        client,
        "Old but still running",
        headers={"X-Client-ID": "retention-tester"},
    ).json()["id"]
    store.update_run_status(old_running, RunStatus.RUNNING)
    _backdate_completion(isolated_db, old_running, 120 * 86_400)

    recent_completed = _create_run(
        client, "Just completed", headers={"X-Client-ID": "retention-tester"}
    ).json()["id"]
    store.update_run_status(recent_completed, RunStatus.COMPLETED)

    deleted = retention.sweep_expired_runs()

    assert deleted == [old_completed]
    assert not store.run_exists(old_completed)
    assert store.run_exists(old_running)
    assert store.run_exists(recent_completed)


def test_sweep_expired_documents_deletes_only_past_the_window(
    isolated_db: str,
) -> None:
    client = make_client()
    staged = client.post(
        "/api/documents",
        headers={"X-Client-ID": "retention-tester"},
        files={"file": ("notes.txt", io.BytesIO(b"old notes"), "text/plain")},
        data={"consent": "true"},
    )
    document_id = staged.json()["id"]

    far_future = time.time() + 200 * 86_400
    deleted = retention.sweep_expired_documents(now=far_future)

    assert deleted == 1
    assert documents.get_staged_documents([document_id], "retention-tester") == []


def _run(db: str) -> str:
    return seed_run("goal", provider="mock", db_path=db).id


def test_save_and_get_latest_checkpoint(isolated_db: str) -> None:
    run_id = _run(isolated_db)
    assert store_checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db) is None
    assert not store_checkpoints.has_checkpoint(run_id, db_path=isolated_db)

    seq1 = seed_checkpoint(
        run_id,
        {"hyp_ids": ["a", "b"]},
        stage="post_generation",
        last_event_seq=5,
        db_path=isolated_db,
    )
    seq2 = seed_checkpoint(
        run_id,
        {"hyp_ids": ["a", "b"], "round": 1},
        stage="post_ranking",
        last_event_seq=12,
        db_path=isolated_db,
    )
    assert (seq1, seq2) == (1, 2)
    assert store_checkpoints.has_checkpoint(run_id, db_path=isolated_db)

    latest = store_checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert latest is not None
    assert latest["seq"] == 2
    assert latest["stage"] == "post_ranking"
    assert latest["last_event_seq"] == 12
    assert latest["state"] == {"hyp_ids": ["a", "b"], "round": 1}


def _count(db: str) -> int:
    with _store_db.connect(db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0])


def _seed_raw_checkpoints(db: str, run_id: str, count: int) -> None:
    with _store_db.connect(db) as conn:
        for i in range(1, count + 1):
            conn.execute(
                "INSERT INTO checkpoints (run_id, seq, stage, schema_version, "
                "last_event_seq, state_json, created_at) "
                "VALUES (?, ?, ?, 1, ?, ?, 0.0)",
                (run_id, i, f"stage_{i}", i, f'{{"round": {i}}}'),
            )


def test_pruning_is_per_run(isolated_db: str) -> None:
    first, second = _run(isolated_db), _run(isolated_db)
    for run_id in (first, second):
        for i in range(3):
            seed_checkpoint(
                run_id,
                {"run": run_id, "round": i},
                stage=f"s{i}",
                last_event_seq=i,
                db_path=isolated_db,
            )

    assert _count(isolated_db) == 2
    for run_id in (first, second):
        latest = store_checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
        assert latest is not None
        assert latest["state"]["run"] == run_id
        assert latest["seq"] == 3


def test_prune_superseded_reclaims_pre_existing_history(
    isolated_db: str,
) -> None:
    # Direct inserts reproduce old databases that bypassed checkpoint pruning.
    run_id = _run(isolated_db)
    _seed_raw_checkpoints(isolated_db, run_id, 20)
    assert _count(isolated_db) == 20

    deleted = store_checkpoints.prune_superseded_checkpoints(db_path=isolated_db)

    assert deleted == 19
    assert _count(isolated_db) == 1
    latest = store_checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert latest is not None
    assert latest["seq"] == 20
    assert latest["state"] == {"round": 20}


def test_prune_batches_and_folds_the_wal_between_batches(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reclamation must survive SQLITE_FULL; commit small batches so headroom
    # shortages cannot roll back all progress.
    from co_scientist.platform.db import checkpoints as checkpoints_module

    run_id = _run(isolated_db)
    _seed_raw_checkpoints(isolated_db, run_id, 11)

    checkpoints: list[int] = []
    real_checkpoint_wal = _store_db.checkpoint_wal

    def _record(db_path: str | None = None) -> None:
        checkpoints.append(_count(isolated_db))
        real_checkpoint_wal(db_path)

    monkeypatch.setattr(checkpoints_module, "checkpoint_wal", _record)

    deleted = store_checkpoints.prune_superseded_checkpoints(db_path=isolated_db)

    assert deleted == 10
    assert _count(isolated_db) == 1
    assert len(checkpoints) >= 2
    assert checkpoints == sorted(checkpoints, reverse=True)
    latest = store_checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert latest is not None
    assert latest["seq"] == 11


def test_every_node_in_node_to_agent_has_an_activity() -> None:
    from co_scientist.orchestration.registry import NODE_TO_AGENT

    for node_name in NODE_TO_AGENT:
        activity = activity_for_event(node_name, {})
        assert activity in ACTIVITY_VALUES
        assert activity != ACTIVITY_OTHER, f"node {node_name!r} has no activity mapping"


_SUPPORTED = "IL-6 increases inflammation via STAT3 signaling."


def _finalize(run: Any, db_path: str) -> None:
    _drain(
        report_finalize.finalize_report(
            run.id,
            report_build.ReportRequest(
                research_goal=run.research_goal,
                run_mode="standard",
                provider="engine",
                execution_time=1.0,
                db_path=db_path,
            ),
            emit_event,
        )
    )


def test_finalize_report_persists_knowledge_facts(isolated_db: str) -> None:
    # Use separate hypotheses: the contradicted one is excluded, while the
    # supported one keeps publication viable.
    run = seed_run("kf e2e goal", provider="mock")
    hyp_id = _add(run.id, "Supported", _SUPPORTED, isolated_db)
    contradicted_id = _add(run.id, "Contradicted", _SUPPORTED, isolated_db)
    records.add_claim_evidence(
        NewClaimEvidence(
            run_id=run.id,
            hypothesis_id=hyp_id,
            claim="IL-6 increases inflammation via STAT3 signaling.",
            label="supports",
            supporting=[{"evidence_id": "ev-1", "quote": "IL-6 raises it."}],
            contradicting=[],
            assessor="deterministic-v1",
        ),
        db_path=isolated_db,
    )
    records.add_claim_evidence(
        NewClaimEvidence(
            run_id=run.id,
            hypothesis_id=contradicted_id,
            claim="TREM2 has no role in this pathway.",
            label="contradicts",
            supporting=[],
            contradicting=[{"evidence_id": "ev-2", "quote": "TREM2 is central."}],
            assessor="deterministic-v1",
        ),
        db_path=isolated_db,
    )

    assert reports.list_knowledge_facts(run.id, db_path=isolated_db) == []

    _finalize(run, isolated_db)

    facts = reports.list_knowledge_facts(run.id, db_path=isolated_db)
    by_kind = {row["kind"]: row for row in facts}
    assert set(by_kind) == {"fact", "contradiction"}
    assert by_kind["fact"]["evidence_id"] == "ev-1"
    assert by_kind["fact"]["hypothesis_id"] == hyp_id
    assert "IL6" in by_kind["fact"]["entities"]
    assert by_kind["contradiction"]["evidence_id"] == "ev-2"
    assert "TREM2" in by_kind["contradiction"]["entities"]


def _append(
    isolated_db: str,
    message: str,
    *,
    level: str = "INFO",
    logger_name: str = "app.test",
    run_id: str | None = None,
) -> int:
    return logs.append_log(
        NewLogRecord(
            level=level,
            levelno=int(logging.getLevelName(level)),
            logger_name=logger_name,
            message=message,
            run_id=run_id,
        ),
        db_path=isolated_db,
    )


def test_append_and_list_roundtrip(isolated_db: str) -> None:
    row_id = logs.append_log(
        NewLogRecord(
            level="INFO",
            levelno=logging.INFO,
            logger_name="app.test",
            message="hello world",
            run_id="run-1",
            exc_text="Traceback: boom",
        ),
        db_path=isolated_db,
    )
    rows = logs.list_logs(db_path=isolated_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == row_id
    assert row["level"] == "INFO"
    assert row["levelno"] == logging.INFO
    assert row["logger"] == "app.test"
    assert row["message"] == "hello world"
    assert row["run_id"] == "run-1"
    assert row["exc_text"] == "Traceback: boom"
    assert row["created_at"] > 0


def test_capture_retention_keeps_the_newest_ordinary_logs(isolated_db: str) -> None:
    for i in range(10):
        _append(isolated_db, f"m{i}")

    assert logs.prune_capture_logs(max_rows=4, db_path=isolated_db) == 7
    rows = logs.list_logs(db_path=isolated_db)
    assert [row["message"] for row in rows] == ["m7", "m8", "m9"]
    assert logs.prune_capture_logs(max_rows=4, db_path=isolated_db) == 0
