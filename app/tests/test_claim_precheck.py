from __future__ import annotations

from typing import Any

import co_scientist.orchestration.repository.tasks as task_store
import pytest
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform import db

from tests._engine_tasks_helpers import _run
from tests._store_helpers import enqueue_task


def _upstream_and_waiter(db_path: str, *, allow_failed: bool = False) -> tuple[str, str, str]:
    run_id = _run()
    upstream = enqueue_task(run_id, "engine.test.upstream", "upstream", db_path=db_path)
    leased = tasks.claim_task("upstream-worker", run_id=run_id, db_path=db_path)
    assert leased is not None and leased.id == upstream.id
    waiter = enqueue_task(
        run_id,
        "engine.test.waiter",
        "waiter",
        dependencies=(upstream.id,),
        provenance={"allow_failed_dependencies": True} if allow_failed else None,
        db_path=db_path,
    )
    return run_id, upstream.id, waiter.id


def _count_write_claims(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    opened = [0]
    real = db.transaction

    def counting(*args: Any, **kwargs: Any) -> Any:
        opened[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(task_store, "transaction", counting)
    return opened


def test_a_task_waiting_on_upstream_work_opens_no_write_transaction(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, upstream_id, waiter_id = _upstream_and_waiter(isolated_db)
    opened = _count_write_claims(monkeypatch)

    assert tasks.claim_task("idle-worker", run_id=run_id, db_path=isolated_db) is None
    assert opened == [0]

    assert lifecycle.complete_task(upstream_id, "upstream-worker", {}, db_path=isolated_db)
    claimed = tasks.claim_task("idle-worker", run_id=run_id, db_path=isolated_db)
    assert claimed is not None and claimed.id == waiter_id


@pytest.mark.parametrize(("allow_failed", "claimable"), [(False, False), (True, True)])
def test_failed_upstreams_admit_only_tasks_that_allow_them(
    isolated_db: str, allow_failed: bool, claimable: bool
) -> None:
    run_id, upstream_id, _ = _upstream_and_waiter(isolated_db, allow_failed=allow_failed)
    with db.connect(isolated_db) as conn:
        conn.execute("UPDATE scientific_tasks SET status='failed' WHERE id=?", (upstream_id,))

    assert lifecycle._has_claimable_task(run_id, isolated_db) is claimable
    claimed = tasks.claim_task("idle-worker", run_id=run_id, db_path=isolated_db)
    assert (claimed is not None) is claimable


def test_a_missing_upstream_is_never_ready(isolated_db: str) -> None:
    run_id = _run()
    enqueue_task(
        run_id,
        "engine.test.waiter",
        "orphan",
        dependencies=("no-such-task",),
        db_path=isolated_db,
    )

    assert lifecycle._has_claimable_task(run_id, isolated_db) is False
