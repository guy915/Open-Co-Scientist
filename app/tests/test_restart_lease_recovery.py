from __future__ import annotations

import time

import co_scientist.main as main_module
import pytest
from co_scientist.orchestration import task_worker
from co_scientist.orchestration.repository import tasks
from co_scientist.platform import db as store_db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import (
    UNKNOWN_PROVIDER_OUTCOME_ERROR,
    RunStatus,
    ScientificTask,
)
from co_scientist.platform.db.runs import RunCreateOptions
from fastapi.testclient import TestClient

from tests._client import DEFAULT_TEST_CLIENT_ID
from tests._store_helpers import enqueue_task, seed_checkpoint, seed_run

# Real owners are "<prefix>.<process tag>:<index>"; the tag is "<pid>.<random>".
_EARLIER_OWNER = "embedded-resume:7.7.earlier:0"
_CURRENT_TAG = "8.current"
_CURRENT_OWNER = f"embedded-api:8.{_CURRENT_TAG}:0"
_RESERVED_MICROEUR = 4_000


def _leased_checkpointed_task(
    title: str, owner: str, db_path: str, *, zero_cost: bool
) -> ScientificTask:
    run = seed_run(
        title,
        config={"zero_cost_admission": True} if zero_cost else {},
        options=RunCreateOptions(client_id=DEFAULT_TEST_CLIENT_ID),
    )
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=db_path)
    seed_checkpoint(run.id, {}, stage="generation", db_path=db_path)
    enqueue_task(
        run.id,
        "engine.fanout.generation.strategy",
        f"strategy:{title}",
        max_attempts=3,
        db_path=db_path,
    )
    leased = tasks.claim_task(owner, run_id=run.id, lease_seconds=300, db_path=db_path)
    assert leased is not None and leased.attempt == 1
    return leased


def _unsettled_paid_call(db_path: str) -> None:
    with store_db.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO llm_spend (id,created_at,model,role,reserved_microeur,"
            "charged_microeur,input_bound,output_bound,rates) "
            "VALUES ('in-flight',?,'azure/model','generation',?,?,1000,1000,'{}')",
            (store_db.current_time(), _RESERVED_MICROEUR, _RESERVED_MICROEUR),
        )


def _paid_call(db_path: str) -> tuple[int, int]:
    with store_db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT charged_microeur, settled FROM llm_spend WHERE id='in-flight'"
        ).fetchone()
    return int(row["charged_microeur"]), int(row["settled"])


def _claim(task: ScientificTask, db_path: str) -> ScientificTask:
    # Startup resume may enqueue sibling work on the same run first.
    for index in range(5):
        claimed = tasks.claim_task(f"next:{index}", run_id=task.run_id, db_path=db_path)
        if claimed is not None and claimed.id == task.id:
            return claimed
    raise AssertionError("the reclaimed task was never claimable")


def _wait_until_not_leased(task_id: str, db_path: str, seconds: float) -> ScientificTask:
    deadline = time.monotonic() + seconds
    while True:
        task = tasks.get_task(task_id, db_path=db_path)
        assert task is not None
        if task.status != "leased" or time.monotonic() > deadline:
            return task
        time.sleep(0.05)


def test_a_new_process_reclaims_leases_a_killed_process_left_within_seconds(
    isolated_db: str, manual_worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The earlier process was killed: no shutdown hook released its leases,
    # and each still has nearly its full 300-second lease left.
    free = _leased_checkpointed_task("free", _EARLIER_OWNER, isolated_db, zero_cost=True)
    paid = _leased_checkpointed_task("paid", _EARLIER_OWNER, isolated_db, zero_cost=False)
    _unsettled_paid_call(isolated_db)
    monkeypatch.setattr(task_worker, "_PROCESS_TAG", _CURRENT_TAG)
    current = _leased_checkpointed_task("current", _CURRENT_OWNER, isolated_db, zero_cost=True)

    started = time.monotonic()
    with TestClient(main_module.app) as client:
        assert client.get("/health").status_code == 200
        reclaimed = _wait_until_not_leased(free.id, isolated_db, seconds=5)
        failed = _wait_until_not_leased(paid.id, isolated_db, seconds=5)
    assert time.monotonic() - started < 10

    # A provably free task is requeued as expiry would requeue it: the lost
    # attempt stays spent, so the retry runs at the next budget.
    assert (reclaimed.status, reclaimed.lease_owner, reclaimed.attempt) == ("queued", None, 1)
    retried = _claim(free, isolated_db)
    assert retried.attempt == 2

    # A paid task may have a billed call in flight: it keeps the unknown
    # outcome, stops its run, and its reserved charge is never released.
    assert failed.status == "failed"
    assert failed.error == UNKNOWN_PROVIDER_OUTCOME_ERROR
    paid_run = runs.get_run(paid.run_id, db_path=isolated_db)
    assert paid_run is not None and paid_run.status == RunStatus.FAILED.value
    assert _paid_call(isolated_db) == (_RESERVED_MICROEUR, 0)

    # A lease this process holds is never taken from it.
    kept = tasks.get_task(current.id, db_path=isolated_db)
    assert kept is not None
    assert (kept.status, kept.lease_owner, kept.attempt) == ("leased", _CURRENT_OWNER, 1)
    assert kept.lease_expires_at == current.lease_expires_at


def test_only_live_leases_of_other_processes_on_unpaused_runs_are_expired(
    isolated_db: str,
) -> None:
    earlier = _leased_checkpointed_task("earlier", _EARLIER_OWNER, isolated_db, zero_cost=True)
    current = _leased_checkpointed_task("current", _CURRENT_OWNER, isolated_db, zero_cost=True)
    paused = _leased_checkpointed_task("paused", _EARLIER_OWNER, isolated_db, zero_cost=True)
    runs.update_run_status(paused.run_id, RunStatus.PAUSED, db_path=isolated_db)

    assert tasks.expire_earlier_process_leases(_CURRENT_TAG, db_path=isolated_db) == 1
    assert tasks.expire_earlier_process_leases(_CURRENT_TAG, db_path=isolated_db) == 0

    requeued = tasks.get_task(earlier.id, db_path=isolated_db)
    assert requeued is not None and requeued.status == "queued"
    kept = tasks.get_task(current.id, db_path=isolated_db)
    assert kept is not None and kept.lease_owner == _CURRENT_OWNER
    # Rescue skips paused runs, and an explicit resume revives an expired
    # lease without the unknown-outcome check, so the lease runs its course.
    held = tasks.get_task(paused.id, db_path=isolated_db)
    assert held is not None and held.lease_expires_at == paused.lease_expires_at
