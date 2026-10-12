from __future__ import annotations

import pytest
from co_scientist.orchestration.engine_tasks import support as engine_tasks_support
from co_scientist.orchestration.engine_tasks.node import _check_portfolio_predecessor
from co_scientist.orchestration.engine_tasks.portfolio import _enqueue_after
from co_scientist.orchestration.engine_tasks.support import SupersededTaskError, TaskCommit
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.orchestration.task_worker import outcomes as task_worker_outcomes
from co_scientist.platform import db
from co_scientist.platform.db import checkpoints, runs
from co_scientist.platform.db.models import RunStatus, ScientificTask

from tests._engine_tasks_helpers import (
    _seed_checkpoint,
    _task_state,
)
from tests._store_helpers import enqueue_task, seed_run


@pytest.mark.parametrize("explicit_cancellation", [False, True])
def test_selected_forecast_edge_recovers_only_run_level_cancellation(
    isolated_db: str, explicit_cancellation: bool
) -> None:
    run = seed_run("Recover portfolio")
    predecessor = _portfolio_seed_predecessor(run.id, isolated_db)
    with db.transaction() as conn:
        successor = _enqueue_after(predecessor, "engine.node.reflection", 90, conn)
        if explicit_cancellation:
            lifecycle.cancel_task(successor.id, reason="superseded branch", conn=conn)
        else:
            lifecycle.cancel_run_tasks(run.id, conn=conn)
        selected = _enqueue_after(predecessor, "engine.node.reflection", 90, conn)
    assert selected.id == successor.id
    assert selected.status == ("cancelled" if explicit_cancellation else "queued")
    if not explicit_cancellation:
        lifecycle.revive_task_for_retry(run.id, predecessor.idempotency_key)
        leased = store.claim_task("resumed", run_id=run.id)
        assert leased is not None and leased.id == predecessor.id
        assert lifecycle.complete_task(predecessor.id, "resumed", {})
        next_task = store.claim_task("resumed", run_id=run.id)
        assert next_task is not None and next_task.id == successor.id


def _portfolio_seed_predecessor(
    run_id: str, db_path: str, task_type: str = "engine.node.generate"
) -> ScientificTask:
    enqueue_task(run_id, task_type, f"{task_type}:seed", db_path=db_path)
    leased = store.claim_task("worker", run_id=run_id, db_path=db_path)
    assert leased is not None
    return leased


@pytest.mark.asyncio
async def test_commit_plans_the_resolvable_tail_behind_the_successor(
    isolated_db: str,
) -> None:
    run = seed_run("Portfolio lookahead")
    predecessor = _portfolio_seed_predecessor(run.id, isolated_db)
    checkpoint_seq = _seed_checkpoint(run.id, _task_state(run.id), db_path=isolated_db)
    commit = TaskCommit(predecessor, checkpoint_seq, isolated_db)

    engine_tasks_support._save_state_and_enqueue(commit, _task_state(run.id), "reflection")

    tasks = {task.task_type: task for task in store.list_tasks(run.id, db_path=isolated_db)}
    reflection = tasks["engine.node.reflection"]
    review = tasks["engine.node.review"]
    assert reflection.status == "queued"
    assert review.status == "queued"
    assert reflection.dependencies == (predecessor.id,)
    assert review.dependencies == (reflection.id,)
    assert reflection.idempotency_key == f"engine.node.reflection:after:{predecessor.id}"
    assert review.idempotency_key == f"engine.node.review:after:{reflection.id}"
    assert "engine.node.comprehensive_reflection" not in tasks


# Cancel whole downstream chains on superseded outcomes; orphan queued rows
# prevent cohort settlement.


def _assert_no_unsatisfiable_dependency(run_id: str, db_path: str) -> None:
    # Queued rows behind failed/cancelled prerequisites are never claimable but
    # keep cohorts alive forever.
    tasks = {t.id: t for t in store.list_tasks(run_id, db_path=db_path)}
    dead = {"cancelled", "failed"}
    for task in tasks.values():
        if task.status != "queued":
            continue
        for dep_id in task.dependencies:
            dep = tasks.get(dep_id)
            if dep is not None and dep.status in dead:
                pytest.fail(
                    f"{task.task_type} ({task.id}) is queued but depends "
                    f"on {dep.task_type} ({dep.id}), status={dep.status}"
                )


@pytest.mark.asyncio
async def test_a_diverging_outcome_cancels_the_whole_downstream_tail(
    isolated_db: str,
) -> None:
    # Use multi-hop divergence; single-hop fixtures cannot expose orphaned
    # descendants.
    run = seed_run("Portfolio deep divergence")
    predecessor = _portfolio_seed_predecessor(
        run.id, isolated_db, task_type="engine.node.orchestrator"
    )
    decided = {**_task_state(run.id), "next_task": "evolve"}
    checkpoint_seq = _seed_checkpoint(run.id, decided, db_path=isolated_db)
    commit = TaskCommit(predecessor, checkpoint_seq, isolated_db)
    committed_seq, _ = engine_tasks_support._save_state_and_enqueue(commit, decided, "meta_review")
    assert lifecycle.complete_task(predecessor.id, "worker", {}, db_path=isolated_db)
    tasks = {task.task_type: task for task in store.list_tasks(run.id, db_path=isolated_db)}
    meta_review = tasks["engine.node.meta_review"]
    evolve = tasks["engine.node.evolve"]
    review = tasks["engine.node.review"]
    assert evolve.status == "queued"
    assert review.status == "queued"
    assert review.dependencies == (evolve.id,), (
        "review must depend on evolve, not on meta_review directly, or "
        "this test is not exercising the transitive case"
    )

    leased_meta_review = store.claim_task("worker", run_id=run.id, db_path=isolated_db)
    assert leased_meta_review is not None
    assert leased_meta_review.id == meta_review.id
    halted_state = {**_task_state(run.id), "safety_blocked": True}
    meta_review_commit = TaskCommit(leased_meta_review, committed_seq, isolated_db)
    engine_tasks_support._save_state_and_enqueue(meta_review_commit, halted_state, None)

    refreshed = {task.task_type: task for task in store.list_tasks(run.id, db_path=isolated_db)}
    assert refreshed["engine.node.evolve"].status == "cancelled"
    assert refreshed["engine.node.review"].status == "cancelled", (
        "review is two hops from the diverging task and must be "
        "cancelled too, or it is permanently unclaimable"
    )
    assert refreshed["engine.finalize"].status == "queued"
    _assert_no_unsatisfiable_dependency(run.id, isolated_db)

    checkpoint = checkpoints.get_latest_checkpoint(run.id, db_path=isolated_db)
    assert checkpoint is not None
    with pytest.raises(SupersededTaskError):
        _check_portfolio_predecessor(review, checkpoint)


def test_a_permanent_failure_cancels_the_downstream_chain(
    isolated_db: str,
) -> None:
    # Permanent failure must cancel dependent chains within settlement's
    # transaction or the run stays nonterminal.
    run = seed_run("Portfolio permanent failure")
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    evolve = enqueue_task(
        run.id,
        "engine.node.evolve",
        "evolve:seed",
        max_attempts=1,
        db_path=isolated_db,
    )
    leased_evolve = store.claim_task("worker", run_id=run.id, db_path=isolated_db)
    assert leased_evolve is not None and leased_evolve.id == evolve.id

    review = enqueue_task(
        run.id,
        "engine.node.review",
        f"engine.node.review:after:{evolve.id}",
        dependencies=(evolve.id,),
        db_path=isolated_db,
    )
    assert review.status == "queued"

    task_worker_outcomes._fail_retryable_task(
        leased_evolve, "worker", RuntimeError("provider error"), isolated_db
    )

    evolve_after = store.get_task(evolve.id, db_path=isolated_db)
    review_after = store.get_task(review.id, db_path=isolated_db)
    assert evolve_after is not None and evolve_after.status == "failed"
    assert review_after is not None and review_after.status == "cancelled", (
        "the downstream row must be cancelled, or it is permanently queued and unclaimable"
    )
    claimable, _, _park = lifecycle.cohort_poll(run.id, db_path=isolated_db)
    assert not claimable, "nothing should read as claimable once settled"

    settled = runs.get_run(run.id, db_path=isolated_db)
    assert settled is not None
    assert settled.status == RunStatus.FAILED.value, (
        "with the orphan gone, fail_task's own settlement check must "
        "find no other claimable work and fail the run rather than "
        "leaving it running with nothing left to advance it"
    )


_META_REVIEW = "engine.node.meta_review"
