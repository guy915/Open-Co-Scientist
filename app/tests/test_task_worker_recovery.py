from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.core.exceptions import (
    LLMTimeoutError,
)
from co_scientist.domains.access import credentials
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform import db as _store_db
from co_scientist.platform import db as store_db
from co_scientist.platform.db import checkpoints, runs
from co_scientist.platform.db.models import RunStatus, ScientificTask

from tests._client import DEFAULT_TEST_CLIENT_ID, make_client
from tests._client import create_run as _create_run
from tests._store_helpers import (
    enqueue_task,
    seed_checkpoint,
    seed_run,
)
from tests._store_helpers import mark_task_leased as _mark_leased


def _commit_checkpoint_successor(
    db: str, run_id: str, writer: ScientificTask, successor_type: str
) -> None:
    with _store_db.transaction(db) as conn:
        checkpoint_seq = seed_checkpoint(
            run_id,
            {"resume_successor": successor_type},
            stage=f"engine_task:{writer.id}",
            last_event_seq=2,
            conn=conn,
        )
        enqueue_task(
            run_id,
            successor_type,
            f"{successor_type}:after:{writer.id}",
            inputs={"checkpoint_seq": checkpoint_seq},
            dependencies=(writer.id,),
            provenance={"scheduled_by": writer.task_type},
            conn=conn,
        )
    assert lifecycle.complete_task(writer.id, "ranking-worker", {}, db_path=db)


@dataclass
class _CheckpointReadInterleaver:
    original_get: Callable[..., dict[str, Any] | None]
    old_checkpoint: dict[str, Any]
    run_id: str
    db: str
    writer: ScientificTask
    successor_type: str
    pool: ThreadPoolExecutor
    future: Future[None] | None = None
    reads: int = 0

    def __call__(
        self,
        check_run_id: str,
        db_path: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        checkpoint = self.original_get(check_run_id, db_path=db_path, conn=conn)
        if check_run_id != self.run_id:
            return checkpoint
        self.reads += 1
        if self.reads == 1:
            self._start_commit(conn)
        if conn is None and self.reads > 1:
            return self.old_checkpoint
        return checkpoint

    def _start_commit(self, conn: sqlite3.Connection | None) -> None:
        self.future = self.pool.submit(
            _commit_checkpoint_successor,
            self.db,
            self.run_id,
            self.writer,
            self.successor_type,
        )
        if conn is None:
            self.future.result(timeout=5)
        else:
            time.sleep(0.05)

    def wait_for_writer(self) -> None:
        assert self.future is not None
        self.future.result(timeout=5)


@pytest.mark.parametrize("writer_state", ["live", "expired", "spent"])
def test_resume_gates_the_checkpoint_successor_on_its_writer_lease(
    isolated_db: str, writer_state: str
) -> None:
    run = seed_run("paused checkpoint writer")
    successor_type = f"{engine_tasks.NODE_TASK_PREFIX}ranking"
    writer = enqueue_task(
        run.id,
        f"{engine_tasks.NODE_TASK_PREFIX}generate",
        "pause:ranking-parent",
        inputs={"checkpoint_seq": 0},
        db_path=isolated_db,
    )
    _mark_leased(
        writer.id,
        isolated_db,
        owner="writer-worker",
        expires_at=time.time() + (3600 if writer_state == "live" else -3600),
        spend_budget=writer_state == "spent",
    )
    runs.update_run_status(run.id, RunStatus.PAUSED, db_path=isolated_db)
    seed_checkpoint(
        run.id,
        {"provider": "engine", "resume_successor": successor_type},
        stage=f"engine_task:{writer.id}",
        last_event_seq=1,
        db_path=isolated_db,
    )
    child = enqueue_task(
        run.id,
        successor_type,
        f"{successor_type}:after:{writer.id}",
        inputs={"checkpoint_seq": 1},
        dependencies=(writer.id,),
        provenance={"scheduled_by": "engine.node.generate"},
        db_path=isolated_db,
    )
    runs.update_run_status(run.id, RunStatus.QUEUED, db_path=isolated_db)

    resumed = task_worker.enqueue_run_workflow(run.id, resume=True, db_path=isolated_db)

    assert resumed.id == child.id
    assert resumed.dependencies == (writer.id,)
    if writer_state == "live":
        assert not tasks.claim_task("early-child", run_id=run.id, db_path=isolated_db)
    else:
        replayed = tasks.claim_task("replacement-worker", run_id=run.id, db_path=isolated_db)
        assert replayed is not None and replayed.id == writer.id
        if writer_state == "spent":
            revived = tasks.get_task(writer.id, db_path=isolated_db)
            assert revived is not None
            assert revived.max_attempts == writer.max_attempts + 1
            assert replayed.attempt == writer.max_attempts + 1
        else:
            assert replayed.attempt == writer.attempt + 1
    assert lifecycle.complete_task(
        writer.id,
        "writer-worker" if writer_state == "live" else "replacement-worker",
        {},
        db_path=isolated_db,
    )
    claimed_child = tasks.claim_task("child-worker", run_id=run.id, db_path=isolated_db)
    assert claimed_child is not None and claimed_child.id == child.id


def test_resume_paused_stage_reuses_recorded_successor_not_writer(
    isolated_db: str,
) -> None:
    run = seed_run("paused checkpoint")
    successor_type = f"{engine_tasks.NODE_TASK_PREFIX}ranking"
    writer = enqueue_task(
        run.id,
        f"{engine_tasks.NODE_TASK_PREFIX}generate",
        "pause:paused-writer",
        inputs={"checkpoint_seq": 0},
        db_path=isolated_db,
    )
    _mark_leased(
        writer.id,
        isolated_db,
        owner="paused-writer",
        expires_at=time.time() + 3600,
        spend_budget=False,
    )
    runs.update_run_status(run.id, RunStatus.PAUSED, db_path=isolated_db)
    seed_checkpoint(
        run.id,
        {"provider": "engine", "resume_successor": successor_type},
        stage=f"engine_task_paused:{writer.id}",
        last_event_seq=1,
        db_path=isolated_db,
    )
    runs.update_run_status(run.id, RunStatus.QUEUED, db_path=isolated_db)

    resumed = task_worker.enqueue_run_workflow(run.id, resume=True, db_path=isolated_db)

    assert resumed.task_type == successor_type
    assert resumed.status == "queued"
    assert resumed.dependencies == (writer.id,)
    assert resumed.id != writer.id
    assert not tasks.claim_task("early-child", run_id=run.id, db_path=isolated_db)
    assert lifecycle.complete_task(writer.id, "paused-writer", {}, db_path=isolated_db)
    claimed = tasks.claim_task("successor-worker", run_id=run.id, db_path=isolated_db)
    assert claimed is not None and claimed.id == resumed.id


def test_resume_serializes_checkpoint_discovery_with_writer_commit(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = seed_run("resume commit race")
    writer = enqueue_task(
        run.id,
        "engine.node.ranking",
        "resume:ranking-writer",
        inputs={"checkpoint_seq": 0},
        db_path=isolated_db,
    )
    _mark_leased(
        writer.id,
        isolated_db,
        owner="ranking-worker",
        expires_at=time.time() + 3600,
        spend_budget=False,
    )
    seed_checkpoint(
        run.id,
        {"provider": "engine"},
        stage=f"engine_task:{writer.id}",
        last_event_seq=1,
        db_path=isolated_db,
    )
    runs.update_run_status(run.id, RunStatus.QUEUED, db_path=isolated_db)
    original_get_checkpoint = checkpoints.get_latest_checkpoint
    old_checkpoint = original_get_checkpoint(run.id, db_path=isolated_db)
    assert old_checkpoint is not None
    successor_type = "engine.node.orchestrator"
    pool = ThreadPoolExecutor(max_workers=1)
    interleaver = _CheckpointReadInterleaver(
        original_get_checkpoint,
        old_checkpoint,
        run.id,
        isolated_db,
        writer,
        successor_type,
        pool,
    )

    monkeypatch.setattr(
        checkpoints,
        "get_latest_checkpoint",
        interleaver,
    )
    try:
        resumed = task_worker.enqueue_run_workflow(run.id, resume=True, db_path=isolated_db)
    finally:
        interleaver.wait_for_writer()
        pool.shutdown()

    assert resumed.id == writer.id
    orchestrators = [
        task
        for task in tasks.list_tasks(run.id, db_path=isolated_db)
        if task.task_type == successor_type and task.status == "queued"
    ]
    assert len(orchestrators) == 1
    assert orchestrators[0].idempotency_key == (f"{successor_type}:after:{writer.id}")


_BYOK_SECRET = "synthetic-timeout-encryption-secret"
_BYOK_KEY = "sk-synthetic-ambiguous-timeout-12345"


def _lease_generate_beside_a_sibling(
    client: Any, db_path: str, worker: str, lease_seconds: float
) -> tuple[str, ScientificTask, ScientificTask]:
    run_id = _create_run(client, "lease loss").json()["id"]
    runs.update_run_status(run_id, RunStatus.RUNNING, db_path=db_path)
    seed_checkpoint(
        run_id,
        {"provider": "engine", "resume_successor": "engine.node.generate"},
        stage="post_generation",
        last_event_seq=1,
        db_path=db_path,
    )
    target = enqueue_task(
        run_id,
        "engine.node.generate",
        "engine.node.generate:1",
        inputs={"checkpoint_seq": 1},
        db_path=db_path,
    )
    sibling = enqueue_task(
        run_id,
        "engine.node.verify",
        "engine.node.verify:1",
        inputs={"checkpoint_seq": 1},
        db_path=db_path,
    )
    leased = tasks.claim_task(worker, run_id=run_id, lease_seconds=lease_seconds, db_path=db_path)
    assert leased is not None and leased.id == target.id
    return run_id, target, sibling


def _assert_unknown_outcome_paused(
    client: Any, run_id: str, target: ScientificTask, sibling: ScientificTask
) -> None:
    body = client.get(f"/api/runs/{run_id}").json()
    task_by_id = {t.id: t for t in tasks.list_tasks(run_id)}
    assert body["status"] == "paused"
    assert body["completed_at"] is None
    assert "may have accepted" in body["error"]
    assert task_by_id[target.id].status == "paused"
    assert task_by_id[sibling.id].status == "paused"
    assert task_by_id[target.id].attempt == 1
    assert task_by_id[target.id].lease_owner is None
    failed_events = [
        event
        for event in store_events.list_events(run_id)
        if event["type"] == "status" and event["payload"]["status"] == "failed"
    ]
    assert failed_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["stored-credential", "paid-to-free-route"])
async def test_an_expired_lease_pauses_until_the_owner_resumes(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    routes = ("model_name", "supervisor_model_name", "chat_model_name")
    credentialed = cause == "stored-credential"
    if credentialed:
        monkeypatch.setattr(settings, "byok_encryption_key", _BYOK_SECRET)
    else:
        for field in (*routes, "semantic_safety_model"):
            monkeypatch.setattr(settings, field, "openrouter/provider/paid")
    dispatches: list[str] = []

    async def _dispatch(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        dispatches.append(task.task_type)
        return {"owner_replay": True}

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", _dispatch)
    with make_client() as client:
        run_id, target, sibling = _lease_generate_beside_a_sibling(
            client, isolated_db, "old-worker", lease_seconds=30
        )
        if credentialed:
            credentials.store_run_credential(
                run_id,
                DEFAULT_TEST_CLIENT_ID,
                byok_scope.ByokCredential(
                    provider="deepseek",
                    api_key=_BYOK_KEY,
                    model="deepseek/deepseek-v4-flash",
                ),
                db_path=isolated_db,
            )
        with _store_db.connect(isolated_db) as conn:
            conn.execute(
                "UPDATE scientific_tasks SET lease_expires_at=0 WHERE id=?",
                (target.id,),
            )
    if not credentialed:
        for field in (*routes, "semantic_safety_model"):
            monkeypatch.setattr(settings, field, "openrouter/nex-agi/nex-n2.5-pro:free")

    with make_client() as restarted:
        _assert_unknown_outcome_paused(restarted, run_id, target, sibling)
        assert not await task_worker.run_once("unacknowledged-worker", db_path=isolated_db)
        assert dispatches == []

        assert restarted.post(f"/api/runs/{run_id}/resume", json={}).status_code == 409
        assert (
            restarted.post(
                f"/api/runs/{run_id}/resume",
                json={"acknowledge_unknown_outcome": True},
                headers={"X-Client-ID": "other-owner"},
            ).status_code
            == 404
        )
        resumed = restarted.post(
            f"/api/runs/{run_id}/resume", json={"acknowledge_unknown_outcome": True}
        )
        assert resumed.status_code == 200
        assert resumed.json()["task_id"] == target.id
        assert (
            restarted.post(
                f"/api/runs/{run_id}/resume", json={"acknowledge_unknown_outcome": True}
            ).status_code
            == 409
        )
        assert await task_worker.run_once("owner-resume", db_path=isolated_db)

    assert dispatches == ["engine.node.generate"]


@pytest.mark.asyncio
async def test_exact_zero_cost_timeout_uses_bounded_delayed_retry(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted: list[int] = []

    async def _timeout_once(_task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        accepted.append(1)
        if len(accepted) == 1:
            raise LLMTimeoutError("provider outcome may be unknown", zero_cost_admitted=True)
        return {"recovered": True}

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", _timeout_once)
    run = seed_run("Exact free timeout")
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    task = enqueue_task(run.id, "engine.node.generate", "generate:seed", db_path=isolated_db)

    assert await task_worker.run_once("free-timeout-worker", db_path=isolated_db)
    queued = tasks.get_task(task.id, db_path=isolated_db)
    assert queued is not None and queued.status == "queued"
    assert queued.attempt == 1
    assert queued.available_at is not None and queued.available_at > store_db.current_time()
    assert not await task_worker.run_once("free-timeout-worker", db_path=isolated_db)
    assert accepted == [1]

    monkeypatch.setattr("co_scientist.platform.db.time.time", lambda: queued.available_at + 1)
    assert await task_worker.run_once("free-timeout-worker", db_path=isolated_db)
    completed = tasks.get_task(task.id, db_path=isolated_db)
    assert completed is not None and completed.status == "completed"
    assert accepted == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize(
    "family,kind",
    [
        ("review", "item"),
        ("reflection", "item"),
        ("verification", "item"),
        ("generation", "strategy"),
    ],
)
async def test_unknown_fanout_outcome_preserves_siblings_and_aggregate(
    manual_worker: None,
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    expired: bool,
    family: str,
    kind: str,
) -> None:
    calls: list[str] = []

    async def dispatch(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        calls.append(task.id)
        if task.id == item.id:
            raise LLMTimeoutError("acceptance unknown")
        return {"continued": True}

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", dispatch)
    with make_client() as client:
        run_id = _create_run(client, "isolated reflection").json()["id"]
        runs.update_run_status(run_id, RunStatus.RUNNING, db_path=isolated_db)
        item = enqueue_task(
            run_id,
            f"engine.fanout.{family}.{kind}",
            "item",
            db_path=isolated_db,
        )
        sibling = enqueue_task(
            run_id,
            f"engine.fanout.{family}.{kind}",
            "sibling",
            db_path=isolated_db,
        )
        aggregate = enqueue_task(
            run_id,
            f"engine.fanout.{family}.aggregate",
            "aggregate",
            dependencies=(item.id, sibling.id),
            provenance={"allow_failed_dependencies": True},
            db_path=isolated_db,
        )
        if expired:
            _mark_leased(
                item.id,
                isolated_db,
                owner="dead-worker",
                expires_at=time.time() - 1,
                spend_budget=False,
            )
        else:
            assert await task_worker.run_once("worker", run_id=run_id, db_path=isolated_db)
        assert await task_worker.run_once("worker", run_id=run_id, db_path=isolated_db)
        assert await task_worker.run_once("worker", run_id=run_id, db_path=isolated_db)
        succeeded = tasks.get_task(sibling.id, db_path=isolated_db)
        assert succeeded is not None and succeeded.status == "completed"
        failed = tasks.get_task(item.id, db_path=isolated_db)
        assert failed is not None and failed.status == "failed"
        assert len(failed.attempts) == 1
        finished = tasks.get_task(aggregate.id, db_path=isolated_db)
        assert finished is not None and finished.status == "completed"
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "running"
        assert calls.count(item.id) == (0 if expired else 1)


@dataclass(frozen=True)
class _ResumeShape:
    stage: str = "post_generation"
    last_event_seq: int = 1
    provider: str | None = None


def _save_resume_checkpoint(
    run_id: str,
    successor: str,
    db: str,
    shape: _ResumeShape | None = None,
) -> None:
    shape = shape or _ResumeShape()
    state: dict[str, Any] = {"resume_successor": successor}
    if shape.provider is not None:
        state["provider"] = shape.provider
    seed_checkpoint(
        run_id,
        state,
        stage=shape.stage,
        last_event_seq=shape.last_event_seq,
        db_path=db,
    )


@pytest.mark.parametrize("recorded", [True, False])
def test_resume_follows_the_recorded_successor_else_the_orchestrator(
    isolated_db: str, recorded: bool
) -> None:
    # Bootstrap checkpoints precede supervisor guidance, so their recorded
    # successor must win; unrecorded legacy and fan-out planning checkpoints
    # already have guidance and re-enter at the orchestrator.
    supervisor_type = f"{engine_tasks.NODE_TASK_PREFIX}supervisor"
    run = seed_run("worker goal")
    enqueued = enqueue_task(
        run.id,
        supervisor_type,
        f"{supervisor_type}:1",
        inputs={"checkpoint_seq": 1},
        db_path=isolated_db,
    )
    if recorded:
        _save_resume_checkpoint(
            run.id,
            supervisor_type,
            isolated_db,
            _ResumeShape(
                stage="engine_task:bootstrap",
                last_event_seq=0,
                provider="engine",
            ),
        )
    else:
        seed_checkpoint(
            run.id,
            {"provider": "engine"},
            stage="engine_task:orchestrator",
            db_path=isolated_db,
        )

    resumed = task_worker.enqueue_run_workflow(run.id, resume=True, db_path=isolated_db)

    if recorded:
        assert resumed.id == enqueued.id, "must resolve to the queued task"
        assert resumed.task_type == supervisor_type
    else:
        assert resumed.task_type == f"{engine_tasks.NODE_TASK_PREFIX}orchestrator"


def test_resume_reuses_post_pause_fanout_rows_for_latest_checkpoint(
    isolated_db: str,
) -> None:
    run = seed_run("paused fanout")
    parent = enqueue_task(
        run.id,
        f"{engine_tasks.NODE_TASK_PREFIX}generate",
        "pause:generate-parent",
        inputs={"checkpoint_seq": 0},
        db_path=isolated_db,
    )
    leased = tasks.claim_task("parent-worker", run_id=run.id, db_path=isolated_db)
    assert leased is not None and leased.id == parent.id
    assert lifecycle.complete_task(parent.id, "parent-worker", {}, db_path=isolated_db)
    runs.update_run_status(run.id, RunStatus.PAUSED, db_path=isolated_db)
    seed_checkpoint(
        run.id,
        {"provider": "engine"},
        stage=f"engine_task:{parent.id}",
        last_event_seq=1,
        db_path=isolated_db,
    )

    stale = enqueue_task(
        run.id,
        f"{engine_tasks.NODE_TASK_PREFIX}review",
        "pause:stale-lookahead",
        inputs={"checkpoint_seq": 0},
        provenance={"scheduled_by": "engine.node.old"},
        db_path=isolated_db,
    )
    fanout = enqueue_task(
        run.id,
        "engine.fanout.generation.strategy",
        "generation:debate_only:1:0:1",
        inputs={"checkpoint_seq": 1},
        dependencies=(parent.id,),
        provenance={"scheduled_by": parent.task_type},
        db_path=isolated_db,
    )
    runs.update_run_status(run.id, RunStatus.QUEUED, db_path=isolated_db)

    resumed = task_worker.enqueue_run_workflow(run.id, resume=True, db_path=isolated_db)

    assert resumed.id == fanout.id
    assert resumed.id != stale.id
    assert not any(
        task.task_type == f"{engine_tasks.NODE_TASK_PREFIX}orchestrator" and task.status == "queued"
        for task in tasks.list_tasks(run.id, db_path=isolated_db)
    )


def _wedge_task_at(run_id: str, task_type: str, checkpoint_seq: int, status: str, db: str) -> str:
    task = enqueue_task(
        run_id,
        task_type,
        f"{task_type}:{checkpoint_seq}",
        inputs={"checkpoint_seq": checkpoint_seq},
        db_path=db,
    )
    with _store_db.connect(db) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET status=?, attempt=max_attempts WHERE id=?",
            (status, task.id),
        )
    return task.id


def _queued(run_id: str, db: str) -> list[Any]:
    return [t for t in tasks.list_tasks(run_id, db_path=db) if t.status == "queued"]


@pytest.mark.parametrize(
    ("status", "revived"),
    [("failed", True), ("cancelled", True), ("succeeded", False)],
)
def test_resume_revives_a_dead_boundary_but_never_completed_work(
    isolated_db: str, status: str, revived: bool
) -> None:
    # An unchanged checkpoint collides with a terminal boundary key, so an
    # explicit resume must revive dead work; reviving a succeeded boundary
    # would repeat committed, paid-for work.
    run = seed_run("wedged goal")
    task_type = f"{engine_tasks.NODE_TASK_PREFIX}orchestrator"
    seed_checkpoint(
        run.id,
        {"resume_successor": task_type},
        stage="post_generation",
        last_event_seq=1,
        db_path=isolated_db,
    )
    task_id = _wedge_task_at(run.id, task_type, 1, status, isolated_db)
    assert not _queued(run.id, isolated_db)

    task_worker.enqueue_run_workflow(run.id, resume=True, db_path=isolated_db)

    queued = _queued(run.id, isolated_db)
    assert [t.id for t in queued] == ([task_id] if revived else [])
    if revived:
        assert queued[0].task_type == task_type
        assert queued[0].attempt < queued[0].max_attempts


@pytest.mark.parametrize(("lease_expires_in", "revived"), [(-3600, True), (3600, False)])
def test_resume_revives_only_a_lease_stranded_by_a_dead_worker(
    isolated_db: str, lease_expires_in: float, revived: bool
) -> None:
    # Expired exhausted leases need explicit recovery (claim rescue skips spent
    # retries); reviving a live lease would run the boundary twice.
    run = seed_run("stranded goal")
    task_type = f"{engine_tasks.NODE_TASK_PREFIX}ranking"
    _save_resume_checkpoint(run.id, task_type, isolated_db)
    task = enqueue_task(
        run.id,
        task_type,
        f"{task_type}:1",
        inputs={"checkpoint_seq": 1},
        db_path=isolated_db,
    )
    _mark_leased(
        task.id,
        isolated_db,
        owner="dead-or-alive",
        expires_at=time.time() + lease_expires_in,
        spend_budget=True,
    )

    task_worker.enqueue_run_workflow(run.id, resume=True, db_path=isolated_db)

    queued = _queued(run.id, isolated_db)
    assert [t.id for t in queued] == ([task.id] if revived else [])
    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None
    assert saved.status == ("queued" if revived else "leased")
    if revived:
        assert saved.attempt < saved.max_attempts
    else:
        assert saved.lease_owner == "dead-or-alive"
