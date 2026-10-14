from __future__ import annotations

from typing import Any

import pytest
from co_scientist.domains.chat.repository import messages
from co_scientist.domains.research_state.drain import hypotheses as drain_hypotheses
from co_scientist.domains.research_state.models import (
    SCIENTIST_REVIEWER,
)
from co_scientist.domains.research_state.repository import hypotheses, records
from co_scientist.domains.research_state.repository.hypotheses import (
    HypothesisStateChanges,
    NewHypothesis,
)
from co_scientist.domains.research_state.repository.records import NewReview
from co_scientist.orchestration import task_worker
from co_scientist.orchestration.engine_tasks import inputs as engine_tasks_inputs
from co_scientist.orchestration.engine_tasks import node as engine_tasks_restore
from co_scientist.orchestration.engine_tasks.support import (
    NODE_TASK_PREFIX,
)
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import tasks as store
from co_scientist.platform.db import checkpoints, runs

from tests._client import create_run as _create_run
from tests._client import make_client as _client
from tests._engine_tasks_helpers import (
    _Generator,
    _seed_checkpoint,
    _task_state,
)
from tests._store_helpers import (
    enqueue_task,
    seed_run,
)
from tests._task_worker_helpers import drain_run_tasks

# Merge scientist ideas only at the orchestrator; growing pools inside ranking
# or fan-out forks state.


def _node_task(run_id: str, node: str, seq: int, db_path: str) -> Any:
    enqueue_task(
        run_id,
        f"{NODE_TASK_PREFIX}{node}",
        f"engine:{node}:{seq}",
        inputs={"checkpoint_seq": seq},
        priority=90,
        db_path=db_path,
    )
    task = store.claim_task("worker", run_id=run_id, db_path=db_path)
    assert task is not None
    return task


def _restored_at(run_id: str, node: str, db_path: str) -> dict[str, Any]:
    state = _task_state(run_id)
    seq = _seed_checkpoint(run_id, state, db_path=db_path)
    task = _node_task(run_id, node, seq, db_path)
    checkpoint = checkpoints.get_latest_checkpoint(run_id, db_path=db_path)
    assert checkpoint is not None
    return engine_tasks_restore._restore_node_task_state(
        task, checkpoint, _Generator(state), {}, db_path
    )


def _seed_hypothesis(run_id: str, db_path: str) -> str:
    hypothesis_id = hypotheses.add_hypothesis(
        NewHypothesis(
            run_id=run_id,
            title="Scientist idea",
            statement="A scientist-proposed mechanism for kinase X.",
            created_by_agent="scientist_manual",
            author="dr-who",
        ),
        db_path=db_path,
    )
    hypotheses.update_hypothesis_state(
        hypothesis_id,
        HypothesisStateChanges(safety_status="allow"),
        db_path=db_path,
    )
    return hypothesis_id


def _seed_review(run_id: str, hypothesis_id: str, verdict: str, db_path: str) -> None:
    records.add_review(
        NewReview(
            run_id=run_id,
            hypothesis_id=hypothesis_id,
            reviewer_agent="scientist",
            summary=f"Scientist verdict: {verdict} (by dr-who)",
            critique="The proposed control cannot distinguish the mechanism.",
            author="dr-who",
            verdict=verdict,
        ),
        db_path=db_path,
    )


@pytest.mark.parametrize(
    ("node", "verdict", "admitted", "rankable"),
    [
        ("orchestrator", None, True, True),
        ("orchestrator", "support", True, True),
        ("orchestrator", "oppose", True, False),
        ("ranking", None, False, False),
    ],
)
def test_scientist_idea_is_admitted_only_at_the_orchestrator_boundary(
    isolated_db: str,
    node: str,
    verdict: str | None,
    admitted: bool,
    rankable: bool,
) -> None:
    from co_scientist.domains.research_state.models import has_peer_review

    run = seed_run("Admission", profile="express")
    hypothesis_id = _seed_hypothesis(run.id, isolated_db)
    if verdict:
        _seed_review(run.id, hypothesis_id, verdict, isolated_db)

    state = _restored_at(run.id, node, isolated_db)

    assert [h.id for h in state["hypotheses"]] == ([hypothesis_id] if admitted else [])
    if admitted:
        merged = state["hypotheses"][0]
        assert merged.origin.value == "scientist_manual"
        # A scientist verdict never stands in for the run's own peer review.
        assert not has_peer_review(merged)
        assert (merged.review_disposition == "inaccurate") is (not rankable)
        assert [r.reviewer for r in merged.reviews] == ([SCIENTIST_REVIEWER] if verdict else [])


def test_authorship_and_screen_survive_a_checkpoint_round_trip(
    isolated_db: str,
) -> None:
    from co_scientist.orchestration.checkpoint import (
        restore_workflow_state,
        serialize_workflow_state,
    )

    run = seed_run("Provenance", profile="express")
    _seed_hypothesis(run.id, isolated_db)
    state = {**_task_state(run.id)}
    engine_tasks_inputs._merge_scientist_inputs(state, run.id, isolated_db)

    envelope = serialize_workflow_state(state, last_event_seq=0)
    restored = restore_workflow_state(envelope, tool_registry=None)

    hypothesis = restored["hypotheses"][0]
    assert hypothesis.origin.value == "scientist_manual"
    assert hypothesis.enrichments["scientist_author"] == "dr-who"
    assert hypothesis.safety_status == "allow"
    assert (
        drain_hypotheses._payload_author(hypothesis.to_dict())
        == hypothesis.enrichments[engine_tasks_inputs.SCIENTIST_AUTHOR_MARK]
    )


def test_an_unscreened_row_does_not_suppress_the_engine_safety_screen(
    isolated_db: str,
) -> None:
    # Pending is a placeholder, not a finished screen; sending it suppresses the
    # real safety decision.
    from co_scientist.science.safety_screen import (
        _screen_one_hypothesis,
    )

    run = seed_run("Unscreened", profile="express")
    hypotheses.add_hypothesis(
        NewHypothesis(
            run_id=run.id,
            title="Unscreened idea",
            statement="A scientist-proposed mechanism for kinase X.",
            created_by_agent="scientist_manual",
            author="dr-who",
        ),
        db_path=isolated_db,
    )

    state = _restored_at(run.id, "orchestrator", isolated_db)

    merged = state["hypotheses"][0]
    assert merged.safety_status is None
    _screen_one_hypothesis(merged)
    assert merged.safety_status == "allow"


def test_the_drain_reattributes_an_idea_whose_row_is_gone(
    isolated_db: str,
) -> None:
    from tests._drain_helpers import _persist

    run = seed_run("Reattribute", profile="express")
    _seed_hypothesis(run.id, isolated_db)
    state: dict[str, Any] = {"hypotheses": []}
    engine_tasks_inputs._merge_scientist_inputs(state, run.id, isolated_db)
    payload = [h.to_dict() for h in state["hypotheses"]]
    payload[0]["id"] = "unseen-engine-id"
    other = seed_run("Fresh store", profile="express")

    _persist(
        run_id=other.id,
        final_state={
            "hypotheses": payload,
            "articles": [],
            "tournament_matchups": [],
            "proximity_graph": {},
            "meta_review": {},
            "research_overview": {},
        },
        db_path=isolated_db,
    )

    rows = hypotheses.list_hypotheses(other.id, isolated_db)
    assert [row["author"] for row in rows] == ["dr-who"]
    assert rows[0]["created_by_agent"] == "scientist_manual"


_LATE_CONTRIB_WORKER = "late-contrib-test"


async def _drain_until_finalize_enqueued(run_id: str, isolated_db: str) -> None:
    for _ in range(500):
        tasks = store.list_tasks(run_id, db_path=isolated_db)
        if any(t.task_type == "engine.finalize" for t in tasks):
            return
        worked = await task_worker.run_once(
            _LATE_CONTRIB_WORKER, run_id=run_id, db_path=isolated_db
        )
        assert worked, "run finished before finalize was ever enqueued"
    raise AssertionError("finalize never appeared inside the task cap")


@pytest.mark.asyncio
async def test_late_contribution_reopens_the_run_once_it_completes(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Contributions arriving after the last orchestrator need a continuation
    # once finalization settles.
    client = _client()
    created = _create_run(client, "Late contribution reopen", tier="express")
    run_id = created.json()["id"]
    started = client.post(f"/api/runs/{run_id}/start", json={})
    assert started.status_code == 200

    await _drain_until_finalize_enqueued(run_id, isolated_db)
    pre_status = runs.get_run(run_id, db_path=isolated_db)
    assert pre_status is not None and pre_status.status != "completed"

    posted = client.post(
        f"/api/runs/{run_id}/messages",
        json={"content": "also consider off-target kinase effects"},
    )
    assert posted.status_code == 200
    assert posted.json()["continuation_task_id"] is None
    assert messages.get_pending_steering(run_id, db_path=isolated_db)

    await drain_run_tasks(run_id, _LATE_CONTRIB_WORKER, db_path=isolated_db)

    reopened = runs.get_run(run_id, db_path=isolated_db)
    assert reopened is not None and reopened.status == "completed", (
        reopened.error if reopened else None
    )
    tasks_after = store.list_tasks(run_id, db_path=isolated_db)
    assert any(
        task.task_type == "engine.node.orchestrator"
        and task.provenance.get("behavior") == "scientist-directed-continuation"
        for task in tasks_after
    )
    lifecycle_events = [
        event["payload"]
        for event in store_events.list_events(run_id, db_path=isolated_db)
        if event["payload"].get("event") == "reopened_for_scientist_input"
    ]
    assert lifecycle_events
    assert not messages.get_pending_steering(run_id, db_path=isolated_db)
