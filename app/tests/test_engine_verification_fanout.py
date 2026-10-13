from __future__ import annotations

import asyncio
from typing import Any

import co_scientist.orchestration.engine_tasks.fanout as engine_tasks_fanout_items
import pytest
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.orchestration import engine_tasks
from co_scientist.orchestration.engine_tasks import fanout as engine_tasks_fanout
from co_scientist.orchestration.engine_tasks import (
    fanout_aggregates as engine_tasks_fanout_aggregates,
)
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform.db import checkpoints
from co_scientist.platform.retrieval.article import Article

from tests._engine_tasks_helpers import (
    _as_finalist,
    _Generator,
    _milestones,
    _patch_generator,
    _seed_checkpoint,
    _task_events,
    _task_state,
)
from tests._store_helpers import enqueue_task, seed_run


def _lease_verification_parent(run_id: str, db_path: str) -> Any:
    parent = enqueue_task(
        run_id,
        f"{engine_tasks.NODE_TASK_PREFIX}deep_verification",
        "verification-parent",
        inputs={"checkpoint_seq": 4},
        db_path=db_path,
    )
    leased = store.claim_task("parent", run_id=run_id, db_path=db_path)
    assert leased is not None and leased.id == parent.id
    return leased


@pytest.mark.asyncio
async def test_a_resumed_run_fans_out_only_the_ideas_still_owed_one(
    isolated_db: str,
) -> None:
    # Once-ever verification markers must survive checkpoints or restarts
    # re-fund the whole pool.
    run = seed_run("Task-level science")
    leased = _lease_verification_parent(run.id, isolated_db)
    state = _task_state(run.id)
    # Depth goes to finalists: reviewed ideas that have played.
    verified = _as_finalist(Hypothesis(text="already verified"))
    verified.enrichments["deep_verification_issued"] = True
    fresh = _as_finalist(Hypothesis(text="an evolution child"))
    state["hypotheses"] = [
        Hypothesis.from_dict(verified.to_dict()),
        Hypothesis.from_dict(fresh.to_dict()),
    ]

    result = engine_tasks_fanout._enqueue_verification_fanout(leased, state, 4, db_path=isolated_db)

    assert len(result["fanout_task_ids"]) == 1
    assert lifecycle.complete_task(leased.id, "parent", result, db_path=isolated_db)
    item = store.claim_task("verify", run_id=run.id, db_path=isolated_db)
    assert item is not None
    assert item.inputs["hypothesis_id"] == state["hypotheses"][1].id


@pytest.mark.asyncio
async def test_a_pool_with_nothing_left_to_verify_advances_into_ranking(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty fan-outs still need an immediately claimable aggregate that commits
    # and advances the run.
    run = seed_run("Task-level science")
    scheduled = await _advance_verification_node(run.id, monkeypatch, isolated_db, issued=True)
    assert scheduled["fanout_task_ids"] == []

    aggregate = store.claim_task("aggregate", run_id=run.id, db_path=isolated_db)
    assert aggregate is not None
    result = await engine_tasks_fanout_aggregates.execute_verification_aggregate(
        aggregate, db_path=isolated_db
    )

    assert result["successful_verifications"] == 0
    assert result["failed_verifications"] == 0
    assert lifecycle.complete_task(aggregate.id, "aggregate", result, db_path=isolated_db)
    successor = store.get_task(result["successor_task_id"], db_path=isolated_db)
    assert successor is not None
    assert successor.task_type == f"{engine_tasks.NODE_TASK_PREFIX}ranking"


async def _fake_verify(*_: Any, **__: Any) -> dict[str, Any]:
    return {
        "probes": [{"question": "q"}],
        "verdict": "holds",
        "retrieval_queries": ["probe query"],
        "retrieved_articles": [
            Article(
                title="Probe evidence",
                source_id="probe-1",
                abstract="Direct targeted support.",
                used_in_analysis=True,
            ).to_dict()
        ],
        "verification_llm_calls": 2,
    }


async def _advance_verification_node(
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
    db_path: str,
    *,
    issued: bool = False,
) -> dict[str, Any]:
    state = _task_state(run_id)
    state["hypotheses"] = [
        _as_finalist(Hypothesis(text=f"candidate-{index}", elo_rating=1200 + index))
        for index in range(3)
    ]
    for hypothesis in state["hypotheses"]:
        hypothesis.enrichments["deep_verification_issued"] = issued
    checkpoint_seq = _seed_checkpoint(run_id, state)
    node = enqueue_task(
        run_id,
        f"{engine_tasks.NODE_TASK_PREFIX}deep_verification",
        "verification-node",
        inputs={"checkpoint_seq": checkpoint_seq},
        db_path=db_path,
    )
    _patch_generator(monkeypatch, _Generator(state), restore=True)
    leased_node = store.claim_task("node", run_id=run_id, db_path=db_path)
    assert leased_node is not None and leased_node.id == node.id
    scheduled = await engine_tasks.execute_node_task(leased_node, db_path=db_path)
    assert lifecycle.complete_task(leased_node.id, "node", scheduled, db_path=db_path)
    return scheduled


async def _run_verification_children_and_aggregate(
    run_id: str, db_path: str, before_aggregate: Any | None = None
) -> None:
    children = [
        store.claim_task(f"child-{index}", run_id=run_id, db_path=db_path) for index in range(3)
    ]
    assert all(child is not None for child in children)
    child_results = await asyncio.gather(
        *[
            engine_tasks_fanout_items.execute_verification_item(child, db_path=db_path)
            for child in children
            if child is not None
        ]
    )
    for index, (child, result) in enumerate(zip(children, child_results, strict=True)):
        assert child is not None
        assert lifecycle.complete_task(child.id, f"child-{index}", result, db_path=db_path)
    aggregate = store.claim_task("aggregate", run_id=run_id, db_path=db_path)
    assert aggregate is not None
    if before_aggregate is not None:
        before_aggregate()
    result = await engine_tasks_fanout_aggregates.execute_verification_aggregate(
        aggregate, db_path=db_path
    )
    assert result["successful_verifications"] == 3
    assert lifecycle.complete_task(aggregate.id, "aggregate", result, db_path=db_path)


def _assert_verifications_are_marked_once_ever(run_id: str, db_path: str) -> None:
    # Aggregate boundaries see the whole family and persist attempt markers even
    # when items fail.
    latest = checkpoints.get_latest_checkpoint(run_id, db_path=db_path)
    assert latest is not None
    restored = latest["state"]["state"]["hypotheses"]
    assert all(item["enrichments"]["deep_verification_issued"] for item in restored)


def _assert_verification_committed(run_id: str, db_path: str) -> None:
    latest = checkpoints.get_latest_checkpoint(run_id, db_path=db_path)
    assert latest is not None
    restored = latest["state"]["state"]["hypotheses"]
    assert all(item["deep_verification_verdict"] == "holds" for item in restored)
    assert latest["state"]["state"]["articles"][-1]["source_id"] == "probe-1"
    assert restored[0]["enrichments"]["deep_verification"]["retrieval_queries"] == ["probe query"]
    assert _milestones(run_id, db_path=db_path) == ["3 hypotheses verified"]
    verification_events = _task_events(run_id, "deep_verification", db_path=db_path)
    assert len(verification_events) == 1
    assert verification_events[0]["payload"]["successor"] == "ranking"


def _assert_fingerprints_survive_the_checkpoint(run_id: str, db_path: str) -> None:
    # Verification fingerprints must survive checkpoints or every later cycle
    # repeats the same paid work.
    from co_scientist.domains.research_state.models import Hypothesis
    from co_scientist.science.reflection.deep_verification import (
        verification_fingerprint,
    )

    latest = checkpoints.get_latest_checkpoint(run_id, db_path=db_path)
    assert latest is not None
    state = latest["state"]["state"]
    for payload in state["hypotheses"]:
        hypothesis = Hypothesis.from_dict(payload)
        assert hypothesis.deep_verification_fingerprint == (
            verification_fingerprint(hypothesis, state["model_name"])
        )


@pytest.mark.asyncio
async def test_verification_children_commit_through_single_aggregator(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("Task-level science")
    await _advance_verification_node(run.id, monkeypatch, isolated_db)

    import co_scientist.science.reflection as reflection

    monkeypatch.setattr(reflection, "verify_hypothesis", _fake_verify)
    await _run_verification_children_and_aggregate(run.id, isolated_db)

    _assert_verification_committed(run.id, isolated_db)
    _assert_fingerprints_survive_the_checkpoint(run.id, isolated_db)
    _assert_verifications_are_marked_once_ever(run.id, isolated_db)


@pytest.mark.asyncio
async def test_failed_verification_items_record_explicit_unverified(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Failed verification is explicitly unverified with stale fingerprints;
    # spend its once-ever marker anyway.
    run = seed_run("Task-level science")
    await _advance_verification_node(run.id, monkeypatch, isolated_db)

    for index in range(3):
        item = store.claim_task(f"child-{index}", run_id=run.id, db_path=isolated_db)
        assert item is not None
        assert store.fail_task(
            item.id,
            f"child-{index}",
            "provider failed",
            retryable=False,
            db_path=isolated_db,
        )

    aggregate = store.claim_task("aggregate", run_id=run.id, db_path=isolated_db)
    assert aggregate is not None
    result = await engine_tasks_fanout_aggregates.execute_verification_aggregate(
        aggregate, db_path=isolated_db
    )
    assert result["successful_verifications"] == 0
    assert result["failed_verifications"] == 3
    assert lifecycle.complete_task(aggregate.id, "aggregate", result, db_path=isolated_db)

    latest = checkpoints.get_latest_checkpoint(run.id, db_path=isolated_db)
    assert latest is not None
    restored = latest["state"]["state"]["hypotheses"]
    assert all(item["deep_verification_verdict"] == "unverified" for item in restored)
    assert all(item["deep_verification_probes"] == [] for item in restored)
    assert all(item["deep_verification_fingerprint"] is None for item in restored)
    assert all(item["enrichments"]["deep_verification_issued"] for item in restored)
