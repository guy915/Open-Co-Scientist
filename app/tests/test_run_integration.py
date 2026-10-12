from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from co_scientist.core.config import settings
from co_scientist.domains.chat import seed
from co_scientist.domains.chat.repository import messages
from co_scientist.domains.chat.repository.messages import NewMessage
from co_scientist.domains.report import build as report_build
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state.repository import hypotheses as store_hypotheses
from co_scientist.domains.research_state.repository import records
from co_scientist.orchestration.engine_tasks import inputs as engine_tasks_inputs
from co_scientist.orchestration.engine_tasks import node as engine_tasks_node
from co_scientist.orchestration.engine_tasks import report_finalize
from co_scientist.orchestration.engine_tasks import support as engine_tasks_support
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import runs_views as views
from co_scientist.orchestration.repository import tasks as store_tasks
from co_scientist.platform import db
from co_scientist.platform.db import runs as store
from co_scientist.platform.db.models import DEMO_CLIENT_ID, RunRow, RunStatus
from fastapi import FastAPI
from httpx import ASGITransport

from tests._client import (
    DEFAULT_TEST_CLIENT_ID,
)
from tests._client import create_run as _create_run
from tests._client import drain as _drain
from tests._client import make_client as _client
from tests._client import wait_for_status as _wait_status
from tests._drain_helpers import (
    _engine_hypothesis,
    _final_state_with_features,
    _persist,
    emit_event,
)
from tests._store_helpers import drive_offline_run, seed_checkpoint, seed_run


@pytest.mark.parametrize("degraded", [["meta_review"], []])
def test_degraded_sections_ride_the_drain_into_the_report(
    isolated_db: str, degraded: list[str]
) -> None:
    run = seed_run("degraded goal")
    state = _final_state_with_features()
    state["degraded_nodes"] = degraded

    drained = _persist(run_id=run.id, final_state=state, db_path=isolated_db)
    _drain(
        report_finalize.finalize_report(
            run.id,
            report_build.ReportRequest(
                research_goal=run.research_goal,
                run_mode="standard",
                provider="engine",
                execution_time=1.0,
                db_path=isolated_db,
                **drained.report_inputs,
            ),
            emit_event,
        )
    )

    assert drained.report_inputs["degraded_sections"] == degraded
    report = reports.get_latest_report(run.id, db_path=isolated_db)
    assert report is not None
    assert report["payload"]["degraded_sections"] == degraded


def _make_run(goal: str, isolated_db: str) -> str:
    run = seed_run(goal, profile="default", client_id="c1", db_path=isolated_db)
    return run.id


def test_restart_fails_interrupted_runs_and_keeps_checkpointed_ones_resumable(
    isolated_db: str,
) -> None:
    active = {
        status: _make_run(f"{status.value} goal", isolated_db)
        for status in (
            RunStatus.RUNNING,
            RunStatus.QUEUED,
            RunStatus.SYNTHESIZING,
        )
    }
    for run_id, status in zip(active.values(), active, strict=True):
        store.update_run_status(run_id, status, db_path=isolated_db)
    done = _make_run("done goal", isolated_db)
    store.update_run_status(done, RunStatus.COMPLETED, db_path=isolated_db)
    resumable = active[RunStatus.RUNNING]
    seed_checkpoint(
        resumable,
        {"round": 1},
        stage="post_ranking",
        last_event_seq=7,
        db_path=isolated_db,
    )

    reconciled = views.reconcile_interrupted_runs(db_path=isolated_db)

    assert reconciled["resumable"] == [resumable]
    failed = {active[RunStatus.QUEUED], active[RunStatus.SYNTHESIZING]}
    assert set(reconciled["failed"]) == failed
    for run_id in failed:
        row = store.get_run(run_id, db_path=isolated_db)
        assert row is not None and row.status == RunStatus.FAILED.value
        assert row.error and "restart" in row.error
        assert any(
            e["type"] == "status" and e["payload"].get("status") == "failed"
            for e in store_events.list_events(run_id, db_path=isolated_db)
        )
    kept = store.get_run(resumable, db_path=isolated_db)
    assert kept is not None and kept.status != RunStatus.FAILED.value
    assert any(
        e["type"] == "status" and e["payload"].get("status") == "resumable"
        for e in store_events.list_events(resumable, db_path=isolated_db)
    )
    done_row = store.get_run(done, db_path=isolated_db)
    assert done_row is not None and done_row.status == "completed"
    assert not views.reconcile_interrupted_runs(db_path=isolated_db)["failed"]


def _three_generation_state() -> dict[str, Any]:
    # Two generations distinguish a complete parent walk from one that stops
    # after the first hop.
    chain = [
        ("root-1", None, 0, "generation"),
        ("child-1", "root-1", 1, "evolution"),
        ("grandchild-1", "child-1", 2, "evolution"),
    ]
    return {
        "hypotheses": [
            _engine_hypothesis(
                hid,
                f"Hypothesis {hid} about glioma stem-cell apoptosis.",
                parent_id=parent,
                generation=generation,
                origin=origin,
            )
            for hid, parent, generation, origin in chain
        ],
        "articles": [],
        "tournament_matchups": [],
        "meta_review": {},
        "evolution_details": [],
        "research_overview": {},
    }


def test_evolution_creates_new_rows_with_parent_lineage(
    isolated_db: str,
) -> None:
    # Offline near-duplicate guards may produce no child; deterministic drain
    # fixtures test actual lineage guarantees.
    run = seed_run(
        "Targeted apoptosis in glioma stem cells",
        profile="express",
        client_id=DEFAULT_TEST_CLIENT_ID,
        db_path=isolated_db,
    )
    _persist(
        run_id=run.id,
        final_state=_three_generation_state(),
        db_path=isolated_db,
    )

    hyps = _client().get(f"/api/runs/{run.id}/hypotheses").json()["hypotheses"]

    lineage = {h["id"]: (h["parent_id"], h["generation"]) for h in hyps}
    assert lineage == {
        "root-1": (None, 0),
        "child-1": ("root-1", 1),
        "grandchild-1": ("child-1", 2),
    }


# Use one async loop for concurrent SSE observation; TestClient background
# execution can finish before return.


def _asgi_app() -> FastAPI:
    # Import settings only after isolated_db establishes its environment.
    from co_scientist.main import app

    return app


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


async def _await_condition(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
    interval: float = 0.005,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met before timeout")


async def _drive_replay_then_live_run(
    isolated_db: str,
) -> tuple[str, httpx.Response, httpx.Response, int]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_asgi_app()),
        base_url="http://test",
        headers={"X-Client-ID": DEFAULT_TEST_CLIENT_ID},
    ) as client:
        create = await client.post(
            "/api/runs",
            json={
                "research_goal": "Integration flow: SSE replay-then-live consistency",
                "tier": "express",
            },
        )
        run_id = create.json()["id"]
        start_task = asyncio.create_task(client.post(f"/api/runs/{run_id}/start", json={}))
        await _await_condition(
            lambda: len(store_events.list_events(run_id, db_path=isolated_db)) >= 3
        )
        events_at_open = len(store_events.list_events(run_id, db_path=isolated_db))
        events_resp = await client.get(f"/api/runs/{run_id}/events", params={"after": 0})
        start_resp = await start_task
    return run_id, events_resp, start_resp, events_at_open


async def test_sse_stream_replay_then_live_matches_full_event_log(
    isolated_db: str,
) -> None:
    (
        run_id,
        events_resp,
        start_resp,
        events_at_open,
    ) = await _drive_replay_then_live_run(isolated_db)

    assert start_resp.status_code == 200
    assert events_resp.status_code == 200
    collected = _parse_sse(events_resp.text)
    assert collected[-1]["type"] == "_terminal"
    assert collected[-1]["payload"]["status"] == "completed"

    non_terminal = collected[:-1]
    stored = store_events.list_events(run_id, db_path=isolated_db)
    assert [e["seq"] for e in non_terminal] == [e["seq"] for e in stored]
    assert [e["type"] for e in non_terminal] == [e["type"] for e in stored]

    assert len(non_terminal) > events_at_open


def test_unverified_completion_notification_stays_disabled(
    isolated_db: str,
) -> None:
    headers = {"X-Client-ID": "notification-scientist"}
    with _client() as client:
        client.headers.update(headers)
        run_id = _create_run(
            client,
            "Study notification fidelity",
            headers=headers,
            tier="express",
            notify_on_completion=True,
            completion_email="scientist@example.org",
        ).json()["id"]
        assert client.post(f"/api/runs/{run_id}/start", headers=headers, json={}).status_code == 200
        assert _wait_status(client, run_id, "completed", timeout=20.0, interval=0.1)

    email_tasks = [
        task
        for task in store_tasks.list_tasks(run_id, db_path=isolated_db)
        if task.task_type == "notification.email"
    ]
    assert not email_tasks


_STEER = "Prioritise chaperone co-expression over temperature shifts"


def _persist_offline_run(isolated_db: str) -> Any:
    return seed_run(
        "Explain how protein X folds under crowding.",
        profile="express",
        provider="mock",
        config={"tier": "express", "enable_literature_review": False},
        client_id="steering-e2e",
        llm_backend="offline",
        db_path=isolated_db,
    )


def _steer_mid_run_then_crash(
    monkeypatch: pytest.MonkeyPatch, run_id: str, db_path: str
) -> dict[str, int]:
    # Lose the first commit carrying steering to test the read-before-checkpoint
    # failure window.
    real = engine_tasks_support._save_state_and_enqueue
    box = {"commits": 0, "crashes": 0}

    def crashing(
        commit: Any,
        state: Any,
        successor: Any,
        *,
        pause_if_requested: bool = False,
    ) -> Any:
        box["commits"] += 1
        if box["commits"] == 2:
            messages.append_message(
                NewMessage(
                    run_id=run_id,
                    sender="user",
                    content=_STEER,
                    kind="steering",
                ),
                db_path=db_path,
            )
        if commit.steering_ids and not box["crashes"]:
            box["crashes"] += 1
            raise RuntimeError("worker lost before the checkpoint committed")
        return real(
            commit,
            state,
            successor,
            pause_if_requested=pause_if_requested,
        )

    for module in (
        engine_tasks_support,
        engine_tasks_inputs,
        engine_tasks_node,
    ):
        monkeypatch.setattr(module, "_save_state_and_enqueue", crashing)
    return box


def test_mid_run_steering_survives_a_crash_and_applies_once(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "semantic_safety_enabled", False)
    run = _persist_offline_run(isolated_db)
    box = _steer_mid_run_then_crash(monkeypatch, run.id, isolated_db)

    drive_offline_run(run, db_path=isolated_db, worker="steering-e2e-worker")

    assert box["crashes"] == 1, "the crash window was never exercised"
    assert messages.get_pending_steering(run.id, db_path=isolated_db) == []
    steers = [
        message
        for message in messages.list_messages(run.id, db_path=isolated_db)
        if message.kind == "steering"
    ]
    assert [message.applied for message in steers] == [True]
    final = store.get_run(run.id, db_path=isolated_db)
    assert final is not None
    assert final.status == RunStatus.COMPLETED.value
    assert reports.get_latest_report(run.id, db_path=isolated_db) is not None


def _fail_one_judged_matchup(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    import co_scientist.science.ranking.operations as ranking_module
    from co_scientist.science.ranking.ranking_debate import judge_matchup as real

    box = {"calls": 0, "failed": 0}

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        box["calls"] += 1
        if box["calls"] == 2:
            box["failed"] += 1
            raise RuntimeError("judge provider refused this matchup")
        return await real(*args, **kwargs)

    monkeypatch.setattr(ranking_module, "judge_matchup", flaky)
    return box


def test_one_failed_matchup_leaves_the_rest_of_the_run_intact(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "semantic_safety_enabled", False)
    run = _persist_offline_run(isolated_db)
    box = _fail_one_judged_matchup(monkeypatch)

    drive_offline_run(run, db_path=isolated_db, worker="steering-e2e-worker")

    assert box["failed"] == 1, "the failure window was never exercised"
    final = store.get_run(run.id, db_path=isolated_db)
    assert final is not None
    assert final.status == RunStatus.COMPLETED.value
    matches = records.list_matches(run.id, db_path=isolated_db)
    assert len(matches) > 1
    # Sibling matchup commits survive one failure; only the failed judgment may
    # rerun.
    assert box["calls"] == len(matches) + 1
    failed_tasks = [
        task
        for task in store_tasks.list_tasks(run.id, db_path=isolated_db)
        if task.status == "failed"
    ]
    assert failed_tasks == []


# Fresh hypothesis identifiers affect later prompts, so run-level offline
# artifact equality is not guaranteed.


def _run_offline_workflow(goal: str, db_path: str) -> tuple[str, list[dict[str, Any]]]:
    run = seed_run(
        goal,
        profile="express",
        config={"tier": "express"},
        llm_backend="offline",
        db_path=db_path,
    )
    drive_offline_run(run, db_path=db_path, worker="offline-workflow-test")
    events = store_events.list_events(run.id, db_path=db_path)
    return run.id, events


def test_offline_workflow_emits_canonical_events_and_completes_with_report(
    isolated_db: str,
) -> None:
    run_id, events = _run_offline_workflow("Sequence test goal", isolated_db)
    types = [e["type"] for e in events]
    nodes = [e["payload"].get("task") for e in events if e["type"] == "scientific_task"]

    expected_gate_events = {
        "safety.intake",
        "safety.hypothesis",
        "citation.grounding",
        "citation_audit",
        "safety.final",
        "report",
    }
    assert expected_gate_events <= set(types), (
        f"missing gate events: {expected_gate_events - set(types)}"
    )

    # Proximity only runs after pool growth; near-duplicate rejection can
    # legitimately leave no pass.
    expected_nodes = {
        "supervisor",
        "generate",
        "review",
        "ranking",
        "evolve",
        "meta_review",
        "deep_verification",
        "research_overview",
    }
    assert expected_nodes <= set(nodes), f"missing nodes: {expected_nodes - set(nodes)}"
    assert types.index("safety.intake") == 0
    assert nodes.index("supervisor") < nodes.index("generate")
    assert types.index("report") == len(types) - 2
    assert events[-1]["type"] == "status"
    assert events[-1]["payload"].get("status") == "completed"

    final = store.get_run(run_id)
    assert final is not None and final.status == RunStatus.COMPLETED.value
    hyps = store_hypotheses.list_hypotheses(run_id, db_path=isolated_db)
    assert hyps
    if any(h.get("parent_id") for h in hyps):
        assert "proximity" in nodes, "missing nodes: {'proximity'}"
    deep = [
        r
        for r in records.list_reviews(run_id, db_path=isolated_db)
        if r["reviewer_agent"] == "deep_verification"
    ]
    assert deep and all(r["summary"] for r in deep)
    report = reports.get_latest_report(run_id, db_path=isolated_db)
    assert report is not None
    assert report["payload"]["leaderboard"]
    assert report["payload"]["provider"] == "engine"
    assert report["payload"].get("research_overview")
    assert "## Research Overview" in report["markdown_text"]


def _seed(db_path: str) -> None:
    asyncio.run(seed.seed_demo_runs(db_path=db_path))


def _demo_runs(db_path: str) -> list[RunRow]:
    return store.list_runs(client_id=DEMO_CLIENT_ID, db_path=db_path)


def _rewrite_config(run_id: str, config: dict[str, Any], db_path: str) -> None:
    with db.connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET config_json=? WHERE id=?",
            (json.dumps(config), run_id),
        )


def test_seed_demo_runs_loads_three_marked_runs_with_their_rows(
    isolated_db: str,
) -> None:
    _seed(isolated_db)

    runs = _demo_runs(isolated_db)
    assert len(runs) == 3
    for run in runs:
        assert run.status == "completed"
        assert run.llm_backend == "offline"
        assert run.config["demo_seed_version"] == run.config["example_chat_version"]
        assert run.config["interview_id"]
        report = reports.get_latest_report(run.id, db_path=isolated_db)
        assert report is not None and "Research Report" in (report["markdown_text"] or "")
        hypotheses = store_hypotheses.list_hypotheses(run.id, db_path=isolated_db)
        assert hypotheses
        assert len(records.list_evidence(run.id, db_path=isolated_db)) == 6
        assert records.list_matches(run.id, db_path=isolated_db)
        assert records.list_reviews(run.id, db_path=isolated_db)
        assert messages.list_messages(run.id, db_path=isolated_db)


def test_seed_demo_runs_is_idempotent_when_runs_are_current(isolated_db: str) -> None:
    _seed(isolated_db)
    before = {
        r.id: reports.get_latest_report(r.id, db_path=isolated_db) for r in _demo_runs(isolated_db)
    }

    _seed(isolated_db)

    after = _demo_runs(isolated_db)
    assert {r.id for r in after} == set(before)
    for run in after:
        report = reports.get_latest_report(run.id, db_path=isolated_db)
        assert report == before[run.id]


def test_seed_demo_runs_replaces_a_stale_run_keeping_its_id(isolated_db: str) -> None:
    _seed(isolated_db)
    stale = _demo_runs(isolated_db)[0]
    _rewrite_config(stale.id, {**stale.config, "example_chat_version": 1}, isolated_db)
    with db.connect(isolated_db) as conn:
        conn.execute("DELETE FROM hypotheses WHERE run_id=?", (stale.id,))

    _seed(isolated_db)

    runs = _demo_runs(isolated_db)
    assert len(runs) == 3
    fresh = next(r for r in runs if r.id == stale.id)
    assert fresh.config["example_chat_version"] == fresh.config["demo_seed_version"]
    assert store_hypotheses.list_hypotheses(stale.id, db_path=isolated_db)
    with db.connect(isolated_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 3


def test_unreadable_demo_snapshot_is_logged_and_never_aborts_startup(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom() -> dict[str, Any]:
        raise RuntimeError("bad snapshot")

    monkeypatch.setattr(seed, "_read_snapshot", _boom)

    with caplog.at_level(logging.ERROR, logger="co_scientist.domains.chat.seed"):
        _seed(isolated_db)

    assert "Failed to read the demo run snapshot" in caplog.text
    assert _demo_runs(isolated_db) == []


def test_failed_demo_run_load_does_not_stop_the_other_examples(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real = seed._load_run
    calls: list[str] = []

    def _flaky(snapshot: dict[str, Any], run_row: dict[str, Any], *rest: Any) -> None:
        calls.append(run_row["id"])
        if len(calls) == 1:
            raise RuntimeError("load failure")
        real(snapshot, run_row, *rest)

    monkeypatch.setattr(seed, "_load_run", _flaky)

    with caplog.at_level(logging.ERROR, logger="co_scientist.domains.chat.seed"):
        _seed(isolated_db)

    assert "Failed to load demo run" in caplog.text
    assert len(_demo_runs(isolated_db)) == 2
