from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import co_scientist.orchestration.engine_tasks.fanout as engine_tasks_fanout_generation
import pytest
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.engine_tasks import node as engine_tasks_node
from co_scientist.orchestration.engine_tasks import ranking as engine_tasks_ranking
from co_scientist.orchestration.engine_tasks import support as engine_tasks_support
from co_scientist.orchestration.engine_tasks.fanout import _GenerationPlan, _StrategyInputs
from co_scientist.orchestration.engine_tasks.fanout_aggregates import _AggregateSpec
from co_scientist.orchestration.engine_tasks.support import ExactSuccessor, TaskCommit
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform import db
from co_scientist.platform.db import checkpoints, runs
from co_scientist.platform.db.models import RunStatus as StoreRunStatus
from co_scientist.platform.llm import ModelCallStats, record_call

from tests._client import create_run as _create_run
from tests._client import make_client
from tests._engine_tasks_helpers import (
    _Generator,
    _milestones,
    _patch_generator,
    _patch_task_node,
    _RankingSeed,
    _run_ranking_node,
    _seed_checkpoint,
    _seed_ranking_node,
    _task_events,
    _task_state,
)
from tests._store_helpers import enqueue_task, seed_run


async def _advance_to_supervisor(
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
    db_path: str,
) -> Any:
    bootstrap = engine_tasks.enqueue_bootstrap(run_id, db_path=db_path)
    leased = store.claim_task("worker", run_id=run_id, db_path=db_path)
    assert leased is not None
    generator = _Generator(_task_state(run_id))
    _patch_generator(monkeypatch, generator, screen=True)
    result = await engine_tasks.execute_bootstrap(leased, db_path=db_path)
    assert lifecycle.complete_task(bootstrap.id, "worker", result, db_path=db_path)
    supervisor = store.claim_task("worker", run_id=run_id, db_path=db_path)
    assert supervisor is not None
    return supervisor


async def _drain_ranking_matches(run_id: str, db_path: str) -> tuple[Any, list[int], int]:
    # Waves observe predecessor checkpoints sequentially; replay a match to
    # exercise idempotency.
    observed_sequences: list[int] = []
    committed = 0
    index = 0
    while True:
        match = store.claim_task(f"match-{index}", run_id=run_id, db_path=db_path)
        assert match is not None
        if match.task_type != engine_tasks_support.RANKING_MATCH_TASK:
            return match, observed_sequences, committed
        observed_sequences.append(int(match.inputs["checkpoint_seq"]))
        result = await engine_tasks_ranking.execute_ranking_match(match, db_path=db_path)
        if index == 0:
            replay = await engine_tasks_ranking.execute_ranking_match(match, db_path=db_path)
            assert replay["replayed"] is True
        committed = int(result["matches_committed"])
        assert lifecycle.complete_task(match.id, f"match-{index}", result, db_path=db_path)
        index += 1


async def _judge_with_telemetry(*_: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    record_call("fixture-model", ModelCallStats(calls=1, prompt_tokens=5))
    return "a", {
        "decision_summary": "A is stronger",
        "confidence_level": "high",
        "debate_turns": int(kwargs["debate_turns"]),
        "debate_transcript": [],
        "judge_model": "fixture-model",
    }


@pytest.mark.asyncio
async def test_ranking_matches_are_separate_sequential_checkpointed_tasks(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.orchestration.checkpoint import restore_workflow_state

    run = seed_run("Task-level science")
    _seed_ranking_node(
        run.id,
        monkeypatch,
        _RankingSeed(
            hypothesis_count=3,
            tournament_pairs=3,
            idempotency_key="ranking-node",
        ),
        isolated_db,
    )
    import co_scientist.science.ranking.operations as ranking_module

    monkeypatch.setattr(ranking_module, "judge_matchup", _judge_with_telemetry)
    await _run_ranking_node(run.id, isolated_db)

    finalizer, observed, committed = await _drain_ranking_matches(run.id, isolated_db)
    assert observed == sorted(set(observed))
    assert committed == 3, "the whole round is judged exactly once"
    result = await engine_tasks_ranking.execute_ranking_finalize(finalizer, db_path=isolated_db)
    assert lifecycle.complete_task(
        finalizer.id,
        finalizer.lease_owner or "finalizer",
        result,
        db_path=isolated_db,
    )

    checkpoint = checkpoints.get_latest_checkpoint(run.id, db_path=isolated_db)
    assert checkpoint is not None
    restored = restore_workflow_state(checkpoint["state"])
    assert len(restored["tournament_matchups"]) == 3
    assert sum(item.total_matches for item in restored["hypotheses"]) == 6
    # Sequential match usage rides successor inputs until the final
    # checkpoint; intermediate commits omit metrics.
    usage = restored["metrics"].model_usage["ranking::fixture-model"]
    assert (usage["calls"], usage["prompt_tokens"]) == (3, 15)
    assert sum(
        task.task_type == engine_tasks_support.RANKING_MATCH_TASK
        for task in store.list_tasks(run.id, db_path=isolated_db)
    ) == len(observed)
    assert _milestones(run.id, db_path=isolated_db) == [
        "Tournament complete (iteration 0, 3 matches)"
    ]
    [event] = _task_events(run.id, "ranking", db_path=isolated_db)
    assert event["payload"]["successor"] == "orchestrator"


@pytest.mark.asyncio
async def test_inflight_pause_checkpoints_exact_successor(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bootstrap already planned a successor; pause must not add another, and
    # resume reuses that row.
    run = seed_run("Task-level science")
    supervisor = await _advance_to_supervisor(run.id, monkeypatch, isolated_db)
    before_pause = store.list_tasks(run.id, db_path=isolated_db)

    async def execute(_name: str, state: dict[str, Any]) -> tuple[dict[str, Any], str]:
        runs.update_run_status(run.id, StoreRunStatus.PAUSED)
        return state, "generate"

    _patch_task_node(monkeypatch, execute)
    paused = await engine_tasks.execute_node_task(supervisor, db_path=isolated_db)
    assert lifecycle.complete_task(supervisor.id, "worker", paused, db_path=isolated_db)
    checkpoint = checkpoints.get_latest_checkpoint(run.id, db_path=isolated_db)
    assert checkpoint is not None
    assert checkpoint["state"]["resume_successor"] == "engine.node.generate"
    assert len(store.list_tasks(run.id, db_path=isolated_db)) == len(before_pause)

    resumed = task_worker.enqueue_run_workflow(run.id, resume=True, db_path=isolated_db)
    assert resumed.task_type == "engine.node.generate"
    assert resumed.id == before_pause[-1].id, "reuses the pre-planned row"


_OWNER = {"X-Client-ID": "cancel-commit-owner"}


def _owned_running_run(db_path: str) -> tuple[Any, str]:
    client = make_client()
    created = _create_run(
        client,
        "Study a durable cancellation boundary",
        headers=_OWNER,
        tier="express",
    )
    assert created.status_code == 200, created.text
    run_id = str(created.json()["id"])
    runs.update_run_status(run_id, StoreRunStatus.RUNNING, db_path=db_path)
    return client, run_id


def _leased_task(run_id: str, task_type: str, key: str, db_path: str) -> tuple[Any, int]:
    checkpoint_seq = _seed_checkpoint(run_id, _task_state(run_id), db_path=db_path)
    queued = enqueue_task(
        run_id,
        task_type,
        key,
        inputs={"checkpoint_seq": checkpoint_seq},
        db_path=db_path,
    )
    task = store.claim_task("cancel-race-worker", run_id=run_id, db_path=db_path)
    assert task is not None and task.id == queued.id
    return task, checkpoint_seq


def _control_run(client: Any, run_id: str, verb: str, status: str) -> None:
    response = client.post(f"/api/runs/{run_id}/{verb}", headers=_OWNER)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status


def _race_node_step(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    race: Callable[[], None],
) -> None:
    real = getattr(engine_tasks_node, name)

    async def raced(*args: Any, **kwargs: Any) -> Any:
        race()
        return await real(*args, **kwargs)

    monkeypatch.setattr(engine_tasks_node, name, raced)


def _prepare_node_without_providers(
    monkeypatch: pytest.MonkeyPatch,
    task: Any,
    checkpoint_seq: int,
    node: str,
    state: dict[str, Any],
    db_path: str,
) -> None:
    monkeypatch.setattr(
        engine_tasks_node,
        "_prepare_node_task",
        lambda *_: (state, TaskCommit(task, checkpoint_seq, db_path), node),
    )


@pytest.mark.asyncio
async def test_cancel_completed_after_node_status_check_blocks_commit(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, run_id = _owned_running_run(isolated_db)
    task, checkpoint_seq = _leased_task(
        run_id, "engine.node.supervisor", "cancel-race-node", isolated_db
    )
    _prepare_node_without_providers(
        monkeypatch,
        task,
        checkpoint_seq,
        "supervisor",
        _task_state(run_id),
        isolated_db,
    )

    async def execute_node(_name: str, state: dict[str, Any]) -> tuple[dict[str, Any], str]:
        return state, "generate"

    _patch_task_node(monkeypatch, execute_node)
    _race_node_step(
        monkeypatch,
        "_commit_node_result",
        lambda: _control_run(client, run_id, "cancel", "cancelled"),
    )

    with pytest.raises(task_worker.LeaseLostError):
        await engine_tasks.execute_node_task(task, db_path=isolated_db)

    latest = checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert latest is not None and latest["seq"] == checkpoint_seq
    assert [row.id for row in store.list_tasks(run_id, db_path=isolated_db)] == [task.id]
    cancelled_task = store.get_task(task.id, db_path=isolated_db)
    assert cancelled_task is not None and cancelled_task.status == "cancelled"
    cancelled_run = runs.get_run(run_id, db_path=isolated_db)
    assert cancelled_run is not None and cancelled_run.status == "cancelled"


def test_pause_transaction_serializes_queued_revocation_with_node_commit(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, run_id = _owned_running_run(isolated_db)
    task, checkpoint_seq = _leased_task(
        run_id, "engine.node.supervisor", "pause-transaction", isolated_db
    )
    commit_finished = Event()
    commit_attempted = Event()
    commit_result: list[tuple[int, str | None]] = []
    commit_errors: list[BaseException] = []

    def commit_successor() -> None:
        try:
            commit_result.append(
                engine_tasks_support._save_state_and_enqueue(
                    TaskCommit(task, checkpoint_seq, isolated_db),
                    _task_state(run_id),
                    "generate",
                    pause_if_requested=True,
                )
            )
        except BaseException as exc:
            commit_errors.append(exc)
        finally:
            commit_finished.set()

    original_transaction = db.transaction
    commit_thread: Thread | None = None

    @contextmanager
    def signal_commit_transaction(
        db_path: str | None = None,
    ) -> Iterator[Any]:
        commit_attempted.set()
        with original_transaction(db_path) as conn:
            yield conn

    monkeypatch.setattr(db, "transaction", signal_commit_transaction)
    with original_transaction(isolated_db) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET status='paused' WHERE run_id=? AND status='queued'",
            (run_id,),
        )
        runs.update_run_status(run_id, StoreRunStatus.PAUSED, conn=conn)
        commit_thread = Thread(target=commit_successor)
        commit_thread.start()
        assert commit_attempted.wait(2), "commit did not reach its transaction"
        assert not commit_finished.wait(0.2), "commit escaped pause transaction"
    assert commit_thread is not None
    commit_thread.join(timeout=5)
    assert not commit_thread.is_alive()
    assert not commit_errors
    assert commit_result == [(checkpoint_seq + 1, None)]
    checkpoint = checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert checkpoint is not None
    assert checkpoint["stage"] == f"engine_task_paused:{task.id}"
    assert not any(row.status == "queued" for row in store.list_tasks(run_id, db_path=isolated_db))


def _commit_node(task: Any, seq: int, run_id: str, db_path: str) -> tuple[int, str | None]:
    return engine_tasks_support._save_state_and_enqueue(
        TaskCommit(task, seq, db_path), _task_state(run_id), "generate"
    )


def _commit_exact(task: Any, seq: int, run_id: str, db_path: str) -> tuple[int, str]:
    return engine_tasks_support._save_state_and_enqueue_exact(
        TaskCommit(task, seq, db_path),
        _task_state(run_id),
        ExactSuccessor(
            task_type="engine.ranking.match",
            inputs={"match_index": 2},
            idempotency_key="match:{checkpoint_seq}",
        ),
    )


def _commit_generation_plan(task: Any, seq: int, run_id: str, db_path: str) -> Any:
    plan = _GenerationPlan(
        task_specs=[],
        inputs=_StrategyInputs(
            literature=None,
            reference_index=SimpleNamespace(text="", sources=[]),
        ),
        aggregate_spec=_AggregateSpec(
            task_type=engine_tasks_support.GENERATION_AGGREGATE_TASK,
            priority=81,
            key_prefix="generation",
        ),
    )
    envelope = {
        "last_event_seq": store_events.latest_event_seq(run_id, db_path=db_path),
        "state": {},
    }
    return engine_tasks_fanout_generation._commit_generation_fanout(
        task, seq, envelope, plan, db_path
    )


def _re_lease_same_owner(run_id: str, stale: Any, db_path: str) -> None:
    with db.transaction(db_path) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET lease_expires_at=0 WHERE id=?",
            (stale.id,),
        )
    assert (
        store.claim_task(stale.lease_owner, lease_seconds=60, run_id=run_id, db_path=db_path)
        is None
    )
    paused = store.get_task(stale.id, db_path=db_path)
    assert paused is not None and paused.status == "paused"
    assert "may have accepted" in (paused.error or "")
    assert lifecycle.resume_run_tasks(run_id, db_path=db_path)
    runs.update_run_status(run_id, StoreRunStatus.QUEUED, db_path=db_path)
    current = store.claim_task(stale.lease_owner, lease_seconds=60, run_id=run_id, db_path=db_path)
    assert current is not None and current.id == stale.id
    assert current.attempt == stale.attempt + 1


@pytest.mark.parametrize("cause", ["run-completed", "re-leased"])
@pytest.mark.parametrize("commit", [_commit_node, _commit_exact, _commit_generation_plan])
def test_a_revoked_task_cannot_commit_at_any_boundary(
    isolated_db: str, commit: Callable[..., Any], cause: str
) -> None:
    _client, run_id = _owned_running_run(isolated_db)
    task, seq = _leased_task(run_id, "engine.node.generate", f"revoked-{cause}", isolated_db)
    if cause == "run-completed":
        runs.update_run_status(run_id, StoreRunStatus.COMPLETED, db_path=isolated_db)
    else:
        _re_lease_same_owner(run_id, task, isolated_db)

    with pytest.raises(task_worker.LeaseLostError):
        commit(task, seq, run_id, isolated_db)

    latest = checkpoints.get_latest_checkpoint(run_id, db_path=isolated_db)
    assert latest is not None and latest["seq"] == seq
    assert [row.id for row in store.list_tasks(run_id, db_path=isolated_db)] == [task.id]
