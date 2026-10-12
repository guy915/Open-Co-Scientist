from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from co_scientist.domains.report import build as report_build
from co_scientist.domains.report import content as report_content
from co_scientist.domains.report import gates as report_gates
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state.claims.gate import ClaimEdge
from co_scientist.domains.research_state.repository import hypotheses, records
from co_scientist.domains.research_state.repository.hypotheses import NewHypothesis
from co_scientist.domains.safety.gate import SafetyDecision
from co_scientist.domains.safety.types import REDACTED_PLACEHOLDER
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.engine_tasks import finalize as engine_tasks_node
from co_scientist.orchestration.engine_tasks import report_finalize
from co_scientist.orchestration.engine_tasks import support as engine_tasks_support
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform.db import checkpoints, runs
from co_scientist.platform.db import retrieval_calls as retrieval
from co_scientist.platform.db.models import RunStatus

from tests._client import create_run as _create_run
from tests._client import make_client
from tests._engine_tasks_helpers import (
    _Generator,
    _install_runtime,
    _patch_restore_generator,
    _seed_checkpoint,
    _task_state,
    fake_final_drain,
)
from tests._store_helpers import (
    enqueue_task,
    pause_run,
    resume_run,
    resume_run_async,
    seed_checkpoint,
)
from tests.test_report_cancel_publication import (
    _OWNER,
    _install_report_stubs,
    _seed_owned_finalize,
)


def _status_event(events: list[dict[str, Any]], status: str) -> dict[str, Any]:
    return next(
        event
        for event in events
        if event["type"] == "status" and event["payload"].get("status") == status
    )


def _statuses(events: list[dict[str, Any]]) -> list[Any]:
    return [event["payload"].get("status") for event in events if event["type"] == "status"]


def _decisions(run_id: str, stage: str, db_path: str) -> list[dict[str, Any]]:
    return [
        item
        for item in records.list_safety_decisions(run_id, db_path=db_path)
        if item["stage"] == stage
    ]


def _hypothesis(identifier: str, status: str) -> dict[str, object]:
    return {
        "id": identifier,
        "title": "An idea",
        "text": "An idea that is both rejected and contradicted.",
        "status": status,
        "safety_status": "allowed",
    }


def _contradicting_edge(hypothesis_id: str) -> ClaimEdge:
    row = {
        "hypothesis_id": hypothesis_id,
        "claim": "The idea contradicts prior data.",
        "label": "contradicts",
        "claim_role": "categorical",
        "supporting": [],
        "contradicting": [],
        "assessor": "llm",
    }
    return ClaimEdge.from_row(row)


@pytest.mark.parametrize(
    ("status", "per_idea", "blocked"),
    [
        ("rejected", "review", "review"),
        ("duplicate", "higher-ranked", "folded into a higher-ranked idea"),
    ],
)
def test_a_contradicted_idea_is_withheld_for_its_status_on_both_surfaces(
    status: str, per_idea: str, blocked: str
) -> None:
    # Status exclusions precede contradiction exclusions across shared report
    # surfaces.
    hyp = _hypothesis("h1", status)
    edges = [_contradicting_edge("h1")]

    contradicted = report_gates.contradicted_hypothesis_ids("run1", None, edges)
    assert "h1" in contradicted
    buckets = report_content._idea_buckets([], [hyp], edges)
    per_idea_reason = buckets["non_viable"][0]["reason"].lower()
    tally = report_gates._exclusion_tally([hyp], [], contradicted)
    blocked_reason = report_gates._empty_leaderboard_reason(1, tally).lower()

    assert per_idea in per_idea_reason
    assert blocked in blocked_reason
    assert "contradicted" not in per_idea_reason
    assert "contradicted" not in blocked_reason


def _seed_leased_finalize(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    client_id: str,
    *,
    monitor_halt: bool = False,
) -> tuple[Any, dict[str, str], str, Any]:
    owner = make_client()
    headers = {"X-Client-ID": client_id}
    created = _create_run(
        owner,
        "Study final safety cancellation",
        headers=headers,
        tier="express",
    )
    assert created.status_code == 200, created.text
    run_id = str(created.json()["id"])
    runs.update_run_status(run_id, RunStatus.RUNNING, db_path=isolated_db)

    state = _task_state(run_id)
    if monitor_halt:
        state["safety_blocked"] = True
        state["safety_decisions"] = [
            {
                "stage": "research_direction",
                "outcome": "prohibited",
                "reason": "Content matches a prohibited policy rule.",
                "matches": ["engineer smallpox for greater transmiss"],
            }
        ]
    checkpoint_seq = _seed_checkpoint(run_id, state, db_path=isolated_db)
    queued = enqueue_task(
        run_id,
        engine_tasks_support.FINALIZE_TASK,
        "final-safety-cancel-readiness",
        inputs={"checkpoint_seq": checkpoint_seq},
        db_path=isolated_db,
    )
    task = tasks.claim_task("final-safety-cancel-worker", run_id=run_id, db_path=isolated_db)
    assert task is not None and task.id == queued.id
    assert task.status == "leased"
    _patch_restore_generator(monkeypatch, _Generator(state))

    _install_runtime(monkeypatch).drain_final_state = fake_final_drain
    return owner, headers, run_id, task


def _install_report(monkeypatch: pytest.MonkeyPatch, *, empty: bool = True) -> None:
    leaderboard = (
        []
        if empty
        else [
            {
                "title": "Hypothesis with sensitive span",
                "statement": "The report contains sensitive span.",
            }
        ]
    )
    built = report_build._BuiltReport(
        payload={"idea_count": 1, "leaderboard": leaderboard},
        markdown="# Goal Report with sensitive span",
        facts=[],
        exclusion_tally={"rejected": 1},
    )

    async def fake_build_report(*_: Any, **__: Any) -> Any:
        return built

    monkeypatch.setattr(report_finalize, "build_report_content", fake_build_report)


def _assert_cancelled_task(
    owner: Any,
    headers: dict[str, str],
    run_id: str,
    task_id: str,
    db_path: str,
) -> list[dict[str, Any]]:
    persisted = runs.get_run(run_id, db_path=db_path)
    assert persisted is not None
    assert persisted.status == RunStatus.CANCELLED.value
    task = tasks.get_task(task_id, db_path=db_path)
    assert task is not None and task.status == "cancelled"
    events = _owner_events(owner, headers, run_id)
    assert _statuses(events) == ["cancelled"]
    return events


def _owner_events(owner: Any, headers: dict[str, str], run_id: str) -> list[dict[str, Any]]:
    response = owner.get(f"/api/runs/{run_id}/events?stream=false", headers=headers)
    assert response.status_code == 200, response.text
    return cast(list[dict[str, Any]], response.json()["events"])


@pytest.mark.parametrize(
    ("gate", "monitor_halt", "stage", "event_type"),
    [
        (
            (report_finalize, "_block_for_empty_leaderboard"),
            False,
            "scientific_readiness",
            None,
        ),
        (
            (engine_tasks_node, "apply_safety_gate"),
            True,
            "research_direction",
            "safety.research_direction",
        ),
    ],
    ids=["readiness-block", "monitor-halt"],
)
@pytest.mark.asyncio
async def test_a_cancel_before_a_blocking_gate_leaves_no_gate_audit(
    manual_worker: None,
    gate: tuple[Any, str],
    monitor_halt: bool,
    stage: str,
    event_type: str | None,
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, headers, run_id, task = _seed_leased_finalize(
        isolated_db,
        monkeypatch,
        f"cancel-before-{stage}",
        monitor_halt=monitor_halt,
    )
    _install_report(monkeypatch)

    async def allow_final_screen(*_: Any, **__: Any) -> Any:
        return SafetyDecision(stage="final", decision="allow")

    _install_runtime(monkeypatch).screen = allow_final_screen
    module, name = gate
    real_gate = getattr(module, name)
    cancel_responses: list[dict[str, Any]] = []

    async def cancel_first(*args: Any, **kwargs: Any) -> Any:
        response = owner.post(f"/api/runs/{run_id}/cancel", headers=headers)
        assert response.status_code == 200, response.text
        cancel_responses.append(response.json())
        async for event in real_gate(*args, **kwargs):
            yield event

    monkeypatch.setattr(module, name, cancel_first)

    with pytest.raises(task_worker.LeaseLostError):
        await engine_tasks.execute_finalize(task, db_path=isolated_db)

    assert cancel_responses == [{"id": run_id, "status": "cancelled"}]
    events = _assert_cancelled_task(owner, headers, run_id, task.id, isolated_db)
    assert _decisions(run_id, stage, isolated_db) == []
    assert not any(event["type"] == event_type for event in events)


@pytest.mark.asyncio
async def test_empty_leaderboard_block_remains_auditable(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, headers, run_id, task = _seed_leased_finalize(
        isolated_db, monkeypatch, "readiness-block-owner"
    )
    _install_report(monkeypatch)

    async def allow_final_screen(*_: Any, **__: Any) -> Any:
        return SafetyDecision(stage="final", decision="allow")

    _install_runtime(monkeypatch).screen = allow_final_screen

    result = await engine_tasks.execute_finalize(task, db_path=isolated_db)

    persisted = runs.get_run(run_id, db_path=isolated_db)
    assert result["status"] == RunStatus.BLOCKED.value
    assert persisted is not None
    assert persisted.status == RunStatus.BLOCKED.value
    readiness = _decisions(run_id, "scientific_readiness", isolated_db)
    assert len(readiness) == 1
    assert readiness[0]["decision"] == "block"

    events = _owner_events(owner, headers, run_id)
    final_event = next(event for event in events if event["type"] == "safety.final")
    blocked_event = _status_event(events, "blocked")
    assert final_event["seq"] < blocked_event["seq"]
    assert not any(event["type"] == "report" for event in events)
    assert reports.get_latest_report(run_id, db_path=isolated_db) is None


@pytest.mark.asyncio
async def test_leased_finalize_redaction_audits_and_scrubs_report(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, headers, run_id, task = _seed_leased_finalize(
        isolated_db, monkeypatch, "final-redaction-owner"
    )
    _install_report(monkeypatch, empty=False)

    async def redact_final_screen(*_: Any, **__: Any) -> Any:
        return SafetyDecision(
            stage="final",
            decision="redact",
            matches=["sensitive span"],
        )

    _install_runtime(monkeypatch).screen = redact_final_screen

    result = await engine_tasks.execute_finalize(task, db_path=isolated_db)

    assert result["status"] == RunStatus.COMPLETED.value
    final_decisions = _decisions(run_id, "final", isolated_db)
    assert len(final_decisions) == 1
    assert final_decisions[0]["decision"] == "redact"
    assert final_decisions[0]["matches"] == ["sensitive span"]

    saved = reports.get_latest_report(run_id, db_path=isolated_db)
    assert saved is not None
    assert "sensitive span" not in repr(saved["payload"]).lower()
    assert REDACTED_PLACEHOLDER in repr(saved["payload"])
    assert "sensitive span" not in saved["markdown_text"].lower()
    assert REDACTED_PLACEHOLDER in saved["markdown_text"]

    events = _owner_events(owner, headers, run_id)
    final_event = next(event for event in events if event["type"] == "safety.final")
    report_event = next(event for event in events if event["type"] == "report")
    completed_event = _status_event(events, "completed")
    assert final_event["seq"] < report_event["seq"] < completed_event["seq"]
    assert "sensitive span" not in repr(report_event["payload"]).lower()


@pytest.mark.asyncio
async def test_leased_monitor_halt_remains_auditable(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, headers, run_id, task = _seed_leased_finalize(
        isolated_db,
        monkeypatch,
        "monitor-halt-audit-owner",
        monitor_halt=True,
    )

    result = await engine_tasks.execute_finalize(task, db_path=isolated_db)

    persisted = runs.get_run(run_id, db_path=isolated_db)
    assert result["status"] == RunStatus.BLOCKED.value
    assert persisted is not None
    assert persisted.status == RunStatus.BLOCKED.value
    monitor = _decisions(run_id, "research_direction", isolated_db)
    assert len(monitor) == 1
    assert monitor[0]["decision"] == "block"
    assert monitor[0]["matches"] == ["engineer smallpox for greater transmiss"]

    events = _owner_events(owner, headers, run_id)
    halt_event = next(event for event in events if event["type"] == "safety.research_direction")
    blocked_event = _status_event(events, "blocked")
    assert halt_event["seq"] < blocked_event["seq"]
    assert not any(event["type"] == "report" for event in events)


@pytest.mark.parametrize("decision", ["allow", "redact"])
@pytest.mark.asyncio
async def test_cancel_during_final_screen_has_no_final_safety_audit(
    manual_worker: None,
    decision: str,
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, headers, run_id, task = _seed_leased_finalize(
        isolated_db, monkeypatch, f"final-screen-cancel-{decision}"
    )
    _install_report(monkeypatch)
    cancel_responses: list[dict[str, Any]] = []

    async def cancel_then_decide(*_: Any, **__: Any) -> Any:
        response = owner.post(f"/api/runs/{run_id}/cancel", headers=headers)
        assert response.status_code == 200, response.text
        cancel_responses.append(response.json())
        matches = ["sensitive span"] if decision == "redact" else []
        return SafetyDecision(stage="final", decision=decision, matches=matches)

    _install_runtime(monkeypatch).screen = cancel_then_decide

    with pytest.raises(task_worker.LeaseLostError):
        await engine_tasks.execute_finalize(task, db_path=isolated_db)

    assert cancel_responses == [{"id": run_id, "status": "cancelled"}]
    events = _assert_cancelled_task(owner, headers, run_id, task.id, isolated_db)
    final_decisions = _decisions(run_id, "final", isolated_db)
    assert final_decisions == []
    assert not any(event["type"] == "safety.final" for event in events)
    assert not any(event["type"] == "report" for event in events)
    assert reports.get_latest_report(run_id, db_path=isolated_db) is None


_DRAIN_STAGES = ("safety.hypothesis", "citation.grounding", "citation_audit")


def _install_final_drain(
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
    hypothesis_id: str,
    isolated_db: str,
    *,
    llm_calls: int,
    during: Any = None,
) -> None:
    _install_runtime(
        monkeypatch
    ).drain_final_state = engine_tasks_node._drain_and_persist_final_state

    async def persist_final_state(*_: Any, **kwargs: Any) -> Any:
        if during is not None:
            during()
        hypotheses.add_hypothesis(
            NewHypothesis(
                run_id=run_id,
                hypothesis_id=hypothesis_id,
                title="IL-6 feedback",
                statement="IL-6 increases inflammation via STAT3 signaling.",
            ),
            db_path=isolated_db,
        )
        kwargs["final_state"]["metrics"] = {"llm_calls": llm_calls}
        return SimpleNamespace(
            safety_counts={},
            grounding_counts={},
            report_inputs={"citation_summary": {}},
        )

    monkeypatch.setattr(engine_tasks_node, "persist_final_state", persist_final_state)


@pytest.mark.asyncio
async def test_cancel_during_final_drain_keeps_cancelled_state(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, run_id, _queued_task, hypothesis_id = _seed_owned_finalize(
        isolated_db, monkeypatch, claim=False
    )
    _install_report_stubs(hypothesis_id, monkeypatch)
    cancel_responses: list[dict[str, Any]] = []

    def cancel() -> None:
        response = owner.post(f"/api/runs/{run_id}/cancel", headers=_OWNER)
        assert response.status_code == 200, response.text
        cancel_responses.append(response.json())

    _install_final_drain(
        monkeypatch,
        run_id,
        hypothesis_id,
        isolated_db,
        llm_calls=1,
        during=cancel,
    )
    task = tasks.claim_task("cancel-during-drain-worker", run_id=run_id, db_path=isolated_db)
    assert task is not None
    with pytest.raises(task_worker.LeaseLostError):
        await engine_tasks.execute_finalize(task, db_path=isolated_db)

    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == RunStatus.CANCELLED.value
    assert cancel_responses == [{"id": run_id, "status": "cancelled"}]
    assert reports.get_latest_report(run_id, db_path=isolated_db) is None
    checkpoint = checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert checkpoint is not None and checkpoint["stage"] == "fixture"
    events = _owner_events(owner, _OWNER, run_id)
    assert not any(event["type"] in {*_DRAIN_STAGES, "report"} for event in events)


@pytest.mark.asyncio
async def test_pause_during_final_drain_waits_for_explicit_resume(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, run_id, original_task, hypothesis_id = _seed_owned_finalize(
        isolated_db, monkeypatch, claim=False
    )
    _install_report_stubs(hypothesis_id, monkeypatch)
    pause_calls: list[str] = []

    def pause_once() -> None:
        if not pause_calls:
            pause_run(run_id, db_path=isolated_db)
            pause_calls.append(run_id)

    _install_final_drain(
        monkeypatch,
        run_id,
        hypothesis_id,
        isolated_db,
        llm_calls=3,
        during=pause_once,
    )

    assert await task_worker.run_once(
        "pause-during-drain-worker", run_id=run_id, db_path=isolated_db
    )
    paused = runs.get_run(run_id, db_path=isolated_db)
    assert pause_calls == [run_id]
    assert paused is not None and paused.status == RunStatus.PAUSED.value
    assert not any(
        row["id"] == hypothesis_id
        for row in hypotheses.list_hypotheses(run_id, db_path=isolated_db)
    )
    assert reports.get_latest_report(run_id, db_path=isolated_db) is None
    pre_resume_events = _owner_events(owner, _OWNER, run_id)
    assert not any(event["type"] in {*_DRAIN_STAGES, "report"} for event in pre_resume_events)
    assert owner.get(f"/api/runs/{run_id}/report", headers=_OWNER).status_code == 404
    checkpoint = checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert checkpoint is not None
    assert checkpoint["stage"] == f"engine_task_paused:{original_task.id}"
    assert checkpoint["state"]["resume_successor"] == engine_tasks_support.FINALIZE_TASK
    assert retrieval.get_run_metrics(run_id, db_path=isolated_db) == {"llm_calls": 3}
    assert tasks.claim_task("before-finalize-resume", run_id=run_id, db_path=isolated_db) is None

    from co_scientist import main

    recovered = main._reconcile_and_log_interrupted_runs()
    assert run_id not in recovered["failed"]
    assert run_id not in recovered["resumable"]
    assert run_id not in tasks.list_active_engine_task_run_ids(db_path=isolated_db)
    assert reports.get_latest_report(run_id, db_path=isolated_db) is None

    await resume_run_async(run_id)
    assert await task_worker.run_once("resumed-finalize-worker", run_id=run_id, db_path=isolated_db)
    completed = runs.get_run(run_id, db_path=isolated_db)
    assert completed is not None
    assert completed.status == RunStatus.COMPLETED.value
    assert reports.get_latest_report(run_id, db_path=isolated_db) is not None

    events = _owner_events(owner, _OWNER, run_id)
    pause_event = next(
        event
        for event in events
        if event["type"] == "lifecycle" and event["payload"].get("event") == "pause_requested"
    )
    resume_event = _status_event(events, "resuming")
    report_event = next(event for event in events if event["type"] == "report")
    completion_event = _status_event(events, "completed")
    assert pause_event["seq"] < resume_event["seq"]
    stages = [
        event
        for event in events
        if event["type"] in {"safety.hypothesis", "citation.grounding", "citation_audit"}
    ]
    assert [event["type"] for event in stages] == [
        "safety.hypothesis",
        "citation.grounding",
        "citation_audit",
    ]
    assert resume_event["seq"] < stages[0]["seq"]
    assert stages[-1]["seq"] < report_event["seq"] < completion_event["seq"]


@pytest.mark.asyncio
async def test_resume_after_finalize_pause_read_does_not_write_stale_checkpoint(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner, run_id, task, hypothesis_id = _seed_owned_finalize(isolated_db, monkeypatch)
    _install_report_stubs(hypothesis_id, monkeypatch)
    previous = checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert previous is not None
    resume_state = {
        **previous["state"],
        "resume_successor": engine_tasks_support.FINALIZE_TASK,
    }
    seed_checkpoint(
        run_id,
        resume_state,
        stage=f"engine_task:{task.id}",
        schema_version=previous["schema_version"],
        last_event_seq=store_events.latest_event_seq(run_id, db_path=isolated_db),
        db_path=isolated_db,
    )
    pause_run(run_id, db_path=isolated_db)
    get_run = runs.get_run
    paused_reads = 0
    resume_calls: list[str] = []

    def resume_after_pause_snapshot(
        requested: str,
        db_path: str | None = None,
        conn: Any | None = None,
    ) -> Any:
        nonlocal paused_reads
        run = get_run(requested, db_path=db_path, conn=conn)
        if requested == run_id and run is not None and run.status == "paused":
            paused_reads += 1
            if paused_reads == 2:
                resume_run(run_id)
                resume_calls.append(run_id)
        return run

    monkeypatch.setattr(runs, "get_run", resume_after_pause_snapshot)
    result = await engine_tasks.execute_finalize(task, db_path=isolated_db)

    assert paused_reads >= 2
    assert resume_calls == [run_id]
    assert result["status"] == RunStatus.COMPLETED.value
    completed = runs.get_run(run_id, db_path=isolated_db)
    assert completed is not None
    assert completed.status == RunStatus.COMPLETED.value
    checkpoint = checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert checkpoint is not None
    assert checkpoint["stage"] == f"engine_task:{task.id}"
    assert reports.get_latest_report(run_id, db_path=isolated_db) is not None
    assert lifecycle.complete_task(
        task.id,
        str(task.lease_owner),
        result,
        db_path=isolated_db,
    )
