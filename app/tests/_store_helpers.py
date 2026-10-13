from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any, TypedDict

from co_scientist.domains.research_state.repository import hypotheses as store
from co_scientist.domains.research_state.repository.hypotheses import NewHypothesis
from co_scientist.orchestration.repository import events, tasks
from co_scientist.orchestration.repository.tasks import NewTask
from co_scientist.platform import db as store_db
from co_scientist.platform.db import checkpoints, runs
from co_scientist.platform.db.checkpoints import NewCheckpoint
from co_scientist.platform.db.models import RunRow, RunStatus, ScientificTask
from co_scientist.platform.db.runs import RunCreateOptions
from typing_extensions import Unpack


def _add(
    run_id: str,
    title: str,
    statement: str,
    db: str,
    mechanism: str = "",
) -> str:
    return store.add_hypothesis(
        NewHypothesis(run_id=run_id, title=title, statement=statement, mechanism=mechanism),
        db_path=db,
    )


def seed_run(
    goal: str,
    *,
    profile: str = "standard",
    provider: str = "engine",
    config: dict[str, Any] | None = None,
    client_id: str = "",
    llm_backend: str | None = None,
    db_path: str | None = None,
    options: RunCreateOptions | None = None,
) -> RunRow:
    return runs.create_run(
        goal,
        profile,
        provider,
        {} if config is None else config,
        options or RunCreateOptions(client_id=client_id, llm_backend=llm_backend, db_path=db_path),
    )


def seed_checkpoint(
    run_id: str,
    state: dict[str, Any],
    *,
    stage: str,
    schema_version: int = 1,
    last_event_seq: int = 0,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    return checkpoints.save_checkpoint(
        run_id,
        NewCheckpoint(
            stage=stage,
            schema_version=schema_version,
            last_event_seq=last_event_seq,
            state=state,
        ),
        db_path=db_path,
        conn=conn,
    )


class _TaskFields(TypedDict, total=False):
    priority: int
    dependencies: Iterable[str]
    provenance: Mapping[str, Any] | None
    budget: Mapping[str, Any] | None
    max_attempts: int


def enqueue_task(
    run_id: str,
    task_type: str,
    key: str,
    *,
    inputs: Mapping[str, Any] | None = None,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
    **fields: Unpack[_TaskFields],
) -> ScientificTask:
    return tasks.enqueue_task(
        NewTask(
            run_id=run_id,
            task_type=task_type,
            inputs={} if inputs is None else inputs,
            idempotency_key=key,
            **fields,
        ),
        db_path=db_path,
        conn=conn,
    )


def mark_task_leased(
    task_id: str,
    db: str,
    *,
    owner: str,
    expires_at: float,
    spend_budget: bool,
) -> None:
    extra = ", attempt=max_attempts" if spend_budget else ""
    with store_db.connect(db) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET status='leased', lease_owner=?, "
            f"lease_expires_at=?{extra} WHERE id=?",
            (owner, expires_at, task_id),
        )


def leased_node_task(run_id: str) -> ScientificTask:
    return ScientificTask(
        id="task-1",
        run_id=run_id,
        task_type="engine.node.generate",
        status="leased",
        priority=90,
        inputs={},
        dependencies=(),
        provenance={},
        idempotency_key="engine.node.generate:0",
        budget={},
        attempt=1,
        max_attempts=3,
        lease_owner="test",
        lease_expires_at=None,
        result=None,
        error=None,
        created_at=0.0,
        updated_at=0.0,
        started_at=None,
        completed_at=None,
    )


def drive_offline_run(run: RunRow, *, db_path: str, worker: str) -> None:
    import asyncio

    from co_scientist.orchestration import task_worker

    task_worker.enqueue_run_workflow(run.id, db_path=db_path)
    asyncio.run(
        task_worker.run_run_worker_pool(
            run.id, worker, policy=task_worker.WorkerPolicy(db_path=db_path)
        )
    )


def event_seqs(
    run_id: str,
    event_type: str,
    *,
    db_path: str | None = None,
    **payload: object,
) -> list[int]:
    from co_scientist.orchestration.repository import events

    return [
        event["seq"]
        for event in events.list_events(run_id, db_path=db_path)
        if event["type"] == event_type
        and all(event["payload"].get(k) == v for k, v in payload.items())
    ]


def pause_run(run_id: str, *, db_path: str | None = None) -> None:
    """Seed the paused state a safety hold leaves: queued tasks parked, run PAUSED."""
    with store_db.transaction(db_path) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET status='paused' WHERE run_id=? AND status='queued'",
            (run_id,),
        )
        runs.update_run_status(run_id, RunStatus.PAUSED, conn=conn)
        events.append_event(run_id, "lifecycle", {"event": "pause_requested"}, conn=conn)


async def resume_run_async(run_id: str) -> None:
    """Drive the resume path the safety-hold release and startup recovery share."""
    import asyncio

    from co_scientist.api.runs import lifecycle as runs_lifecycle

    await runs_lifecycle._launch_resume(run_id)
    await asyncio.gather(*list(runs_lifecycle._resume_tasks))


def resume_run(run_id: str) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    # A fresh thread has no running loop, so callers may sit inside one.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, resume_run_async(run_id)).result()
