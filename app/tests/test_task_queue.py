from __future__ import annotations

import os
import subprocess
import sys
import time

import co_scientist.orchestration.repository.tasks as task_store
import pytest
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus

from tests._engine_tasks_helpers import _enqueue, _run, _three_control_tasks
from tests._store_helpers import enqueue_task

# Lease contention needs real processes; allow for shared CPU contention.
_SUBPROCESS_TIMEOUT_SECONDS = float(os.getenv("COSCIENTIST_TEST_SUBPROCESS_TIMEOUT_SECONDS", "60"))

_CLAIM_SCRIPT = """
import sys
from co_scientist.orchestration.repository import tasks as store
task = store.claim_task(sys.argv[3], run_id=sys.argv[2], db_path=sys.argv[1])
print(task.id if task else "NONE")
"""

_COMPLETE_SCRIPT = """
import sys
from co_scientist.orchestration.repository import tasks_lifecycle as store
completed = store.complete_task(
    sys.argv[2], sys.argv[3], {"value": sys.argv[4]}, db_path=sys.argv[1]
)
print("TRUE" if completed else "FALSE")
"""


def _parallel_scripts(script: str, arguments: list[list[str]]) -> list[str]:
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for args in arguments
    ]
    outputs: list[str] = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=_SUBPROCESS_TIMEOUT_SECONDS)
        assert worker.returncode == 0, stderr
        outputs.append(stdout.strip())
    return outputs


def test_enqueue_is_idempotent(isolated_db: str) -> None:
    run_id = _run()
    first = enqueue_task(
        run_id,
        "generation.observation",
        "generation:a:0",
        inputs={"branch": "a"},
        db_path=isolated_db,
    )
    duplicate = enqueue_task(
        run_id,
        "generation.observation",
        "generation:a:0",
        inputs={"branch": "changed"},
        db_path=isolated_db,
    )
    assert duplicate.id == first.id
    assert duplicate.inputs == {"branch": "a"}
    assert len(tasks.list_tasks(run_id, db_path=isolated_db)) == 1


def test_claim_respects_priority_and_dependencies(isolated_db: str) -> None:
    run_id = _run()
    prerequisite = enqueue_task(
        run_id,
        "retrieval.pubmed",
        "retrieval:0",
        priority=1,
        db_path=isolated_db,
    )
    enqueue_task(
        run_id,
        "reflection.full",
        "review:0",
        priority=100,
        dependencies=[prerequisite.id],
        db_path=isolated_db,
    )
    leased = tasks.claim_task("worker-a", run_id=run_id, db_path=isolated_db)
    assert leased is not None
    assert leased.id == prerequisite.id
    assert lifecycle.complete_task(leased.id, "worker-a", {"evidence": 2}, db_path=isolated_db)
    review = tasks.claim_task("worker-b", run_id=run_id, db_path=isolated_db)
    assert review is not None
    assert review.task_type == "reflection.full"


def test_completion_is_exactly_once(isolated_db: str) -> None:
    run_id = _run()
    task = enqueue_task(run_id, "ranking.debate", "match:a:b:0", db_path=isolated_db)
    leased = tasks.claim_task("worker-a", run_id=run_id, db_path=isolated_db)
    assert leased is not None and leased.id == task.id
    assert lifecycle.complete_task(task.id, "worker-a", {"winner": "a"}, db_path=isolated_db)
    assert not lifecycle.complete_task(task.id, "worker-a", {"winner": "b"}, db_path=isolated_db)
    [saved] = tasks.list_tasks(run_id, db_path=isolated_db)
    assert saved.result == {"winner": "a"}


def test_multi_process_claim_has_single_lease_winner(
    isolated_db: str,
) -> None:
    run_id = _run()
    task = enqueue_task(run_id, "ranking.debate", "multi-process-claim", db_path=isolated_db)

    claimed_ids = _parallel_scripts(
        _CLAIM_SCRIPT,
        [
            [isolated_db, run_id, "worker-0"],
            [isolated_db, run_id, "worker-1"],
        ],
    )

    assert claimed_ids.count(task.id) == 1
    assert claimed_ids.count("NONE") == 1


def test_multi_process_duplicate_completion_commits_one_effect(
    isolated_db: str,
) -> None:
    run_id = _run()
    task = enqueue_task(
        run_id,
        "verification.deep",
        "multi-process-completion",
        db_path=isolated_db,
    )
    leased = tasks.claim_task("shared-worker", run_id=run_id, db_path=isolated_db)
    assert leased is not None

    outcomes = _parallel_scripts(
        _COMPLETE_SCRIPT,
        [
            [isolated_db, task.id, "shared-worker", "first"],
            [isolated_db, task.id, "shared-worker", "second"],
        ],
    )

    assert sorted(outcomes) == ["FALSE", "TRUE"]
    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.result in ({"value": "first"}, {"value": "second"})


def test_crashed_process_lease_is_redelivered_after_restart(
    isolated_db: str,
) -> None:
    run_id = _run()
    task = enqueue_task(
        run_id,
        "evolution.combine",
        "process-crash-redelivery",
        max_attempts=2,
        db_path=isolated_db,
    )
    crash_script = _CLAIM_SCRIPT.replace(
        "run_id=sys.argv[2], db_path=sys.argv[1]",
        "run_id=sys.argv[2], db_path=sys.argv[1], lease_seconds=0.01",
    )

    [claimed] = _parallel_scripts(crash_script, [[isolated_db, run_id, "crashed-worker"]])
    assert claimed == task.id
    time.sleep(0.02)

    recovered = tasks.claim_task("restart-worker", run_id=run_id, db_path=isolated_db)
    assert recovered is not None
    assert recovered.id == task.id
    assert recovered.attempt == 2
    assert lifecycle.complete_task(
        task.id,
        "restart-worker",
        {"recovered": True},
        db_path=isolated_db,
    )


def test_owned_lease_can_be_renewed_without_redelivery(
    isolated_db: str,
) -> None:
    run_id = _run()
    queued = enqueue_task(run_id, "verification.deep", "renew:0", db_path=isolated_db)
    leased = tasks.claim_task("worker-a", lease_seconds=0.01, run_id=run_id, db_path=isolated_db)
    assert leased is not None
    assert lifecycle.renew_task_lease(queued.id, "worker-a", 1.0, db_path=isolated_db)
    time.sleep(0.02)
    assert tasks.claim_task("worker-b", run_id=run_id, db_path=isolated_db) is None
    assert not lifecycle.renew_task_lease(queued.id, "worker-b", 1.0, db_path=isolated_db)


def test_failure_retries_then_stops(isolated_db: str) -> None:
    run_id = _run()
    enqueue_task(
        run_id,
        "verification.deep",
        "verify:0",
        max_attempts=2,
        db_path=isolated_db,
    )
    first = tasks.claim_task("worker-a", run_id=run_id, db_path=isolated_db)
    assert first is not None
    assert tasks.fail_task(first.id, "worker-a", "timeout", db_path=isolated_db)
    second = tasks.claim_task("worker-b", run_id=run_id, db_path=isolated_db)
    assert second is not None and second.attempt == 2
    assert tasks.fail_task(second.id, "worker-b", "timeout", db_path=isolated_db)
    assert tasks.claim_task("worker-c", run_id=run_id, db_path=isolated_db) is None
    [saved] = tasks.list_tasks(run_id, db_path=isolated_db)
    assert saved.status == "failed"


def test_task_progress_is_monotonic_and_budget_derived(
    isolated_db: str,
) -> None:
    run_id = _run()
    first = _enqueue(run_id, "retrieval.pubmed", "progress:retrieval", isolated_db)
    _enqueue(run_id, "generation.initial", "progress:generation", isolated_db)
    initial = runs.task_progress(run_id, db_path=isolated_db)
    assert initial == {
        "determinate": True,
        "completed_tasks": 0,
        "total_tasks": 2,
        "fraction": 0.0,
        "active_task": None,
        "queued_tasks": 2,
    }

    leased = tasks.claim_task("worker", run_id=run_id, db_path=isolated_db)
    assert leased is not None and leased.id == first.id
    active = runs.task_progress(run_id, db_path=isolated_db)
    assert active["fraction"] == 0.0
    assert active["active_task"] == "retrieval.pubmed"

    assert lifecycle.complete_task(first.id, "worker", {"count": 4}, db_path=isolated_db)
    completed = runs.task_progress(run_id, db_path=isolated_db)
    assert completed["fraction"] == 0.5
    assert completed["completed_tasks"] == 1


def test_dynamic_engine_plan_stays_indeterminate_as_tasks_expand(
    isolated_db: str,
) -> None:
    run_id = _run()
    enqueue_task(run_id, "engine.bootstrap", "engine-bootstrap", db_path=isolated_db)
    progress = runs.task_progress(run_id, db_path=isolated_db)
    assert progress["determinate"] is False
    assert progress["fraction"] is None
    assert progress["total_tasks"] == 1


def test_cancel_run_tasks_revokes_queued_leased_and_paused_work(
    isolated_db: str,
) -> None:
    run_id = _run()
    first = enqueue_task(run_id, "engine.node.review", "cancel:first", db_path=isolated_db)
    enqueue_task(run_id, "engine.node.ranking", "cancel:second", db_path=isolated_db)
    enqueue_task(run_id, "engine.node.review", "cancel:paused", db_path=isolated_db)
    assert tasks.claim_task("worker", run_id=run_id, db_path=isolated_db)
    with db.transaction(isolated_db) as conn:
        parked = conn.execute(
            "UPDATE scientific_tasks SET status='paused' WHERE run_id=? AND status='queued'",
            (run_id,),
        ).rowcount
    assert parked == 2
    assert lifecycle.cancel_run_tasks(run_id, db_path=isolated_db) == 3
    assert not lifecycle.complete_task(first.id, "worker", {"late": True}, db_path=isolated_db)
    statuses = {task.status for task in tasks.list_tasks(run_id, db_path=isolated_db)}
    assert statuses == {"cancelled"}


@pytest.mark.parametrize(
    "task_type",
    [
        "engine.bootstrap",
        "engine.fanout.generation.strategy",
        "engine.ranking.match",
        "engine.fanout.generation.aggregate",
    ],
)
def test_paused_run_ignores_late_queue_rows_and_cohort_work(
    isolated_db: str, task_type: str
) -> None:
    run_id = _run()
    predecessor = enqueue_task(
        run_id,
        "engine.node.orchestrator",
        "pause:predecessor",
        db_path=isolated_db,
    )
    leased = tasks.claim_task("predecessor-worker", run_id=run_id, db_path=isolated_db)
    assert leased is not None and leased.id == predecessor.id
    assert lifecycle.complete_task(predecessor.id, "predecessor-worker", {}, db_path=isolated_db)

    active = enqueue_task(run_id, "engine.node.ranking", "pause:active-lease", db_path=isolated_db)
    leased = tasks.claim_task("active-worker", run_id=run_id, db_path=isolated_db)
    assert leased is not None and leased.id == active.id

    with db.transaction(isolated_db) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET status='paused' WHERE run_id=? AND status='queued'",
            (run_id,),
        )
        runs.update_run_status(run_id, RunStatus.PAUSED, conn=conn)

    enqueue_task(
        run_id,
        task_type,
        f"pause:late:{task_type}",
        dependencies=(predecessor.id,),
        db_path=isolated_db,
    )
    delayed = enqueue_task(
        run_id,
        "engine.fanout.review.aggregate",
        "pause:future-due",
        db_path=isolated_db,
    )
    with db.connect(isolated_db) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET available_at=? WHERE id=?",
            (time.time() + 3600, delayed.id),
        )

    assert tasks.claim_task("new-worker", run_id=run_id, db_path=isolated_db) is None
    assert lifecycle.cohort_poll(run_id, db_path=isolated_db) == (
        False,
        True,
        None,
    )
    assert lifecycle.complete_task(active.id, "active-worker", {}, db_path=isolated_db)
    assert lifecycle.cohort_poll(run_id, db_path=isolated_db) == (
        False,
        False,
        None,
    )


def test_claim_rechecks_pause_after_advisory_probe(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _run()
    task = _enqueue(run_id, "engine.node.generate", "pause:racing", isolated_db)
    advisory_probe = task_store._has_claimable_task

    def pause_after_probe(probed_run_id: str | None, db_path: str | None) -> bool:
        claimable = advisory_probe(probed_run_id, db_path)
        runs.update_run_status(run_id, RunStatus.PAUSED, db_path=isolated_db)
        return claimable

    monkeypatch.setattr(task_store, "_has_claimable_task", pause_after_probe)

    assert tasks.claim_task("racing-worker", run_id=run_id, db_path=isolated_db) is None
    still_queued = tasks.get_task(task.id, db_path=isolated_db)
    assert still_queued is not None and still_queued.status == "queued"


def test_paused_cohort_does_not_wait_for_expired_retryable_engine_lease(
    isolated_db: str,
) -> None:
    run_id = _run()
    task = _enqueue(run_id, "engine.node.generate", "pause:expired", isolated_db)
    leased = tasks.claim_task("expired-worker", run_id=run_id, db_path=isolated_db)
    assert leased is not None and leased.id == task.id
    with db.connect(isolated_db) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET lease_expires_at=? WHERE id=?",
            (time.time() - 3600, task.id),
        )
    runs.update_run_status(run_id, RunStatus.PAUSED, db_path=isolated_db)

    assert lifecycle.cohort_poll(run_id, db_path=isolated_db) == (
        False,
        False,
        None,
    )


def test_supervisor_can_reprioritize_cancel_and_retry_individual_tasks(
    isolated_db: str,
) -> None:
    run_id = _run()
    promoted_id, cancelled_id, failed_id = _three_control_tasks(run_id, isolated_db)

    assert lifecycle.reprioritize_task(
        promoted_id,
        99,
        reason="most valuable evidence gap",
        db_path=isolated_db,
    )
    assert lifecycle.cancel_task(
        cancelled_id,
        reason="superseded branch",
        db_path=isolated_db,
    )
    assert lifecycle.retry_task(
        failed_id,
        reason="new evidence available",
        db_path=isolated_db,
    )

    by_id = {task.id: task for task in tasks.list_tasks(run_id, db_path=isolated_db)}
    assert by_id[promoted_id].priority == 99
    assert by_id[cancelled_id].status == "cancelled"
    assert by_id[failed_id].status == "queued"
    assert by_id[failed_id].max_attempts == 2
