from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from co_scientist.core.exceptions import LLMTimeoutError
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.repository import tasks, tasks_lifecycle
from co_scientist.platform.db import connect, runs
from co_scientist.platform.db.models import RunStatus, ScientificTask
from co_scientist.platform.llm import complete_request, offline_guard
from co_scientist.platform.llm.request.azure import LUNA

from tests._azure_ledger import record_azure_allowance
from tests._client import make_operator_client
from tests._llm_fake_backend import completion_response, install_completion_backend
from tests._store_helpers import enqueue_task, mark_task_leased, seed_run

_MODEL = "gpt-4o-mini"
_QUIESCE = "/api/deployment/quiesce"


def _queued_task(db_path: str, label: str) -> tuple[str, ScientificTask]:
    run = seed_run(f"quiesce {label}", db_path=db_path)
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=db_path)
    task = enqueue_task(
        run.id,
        "engine.node.ranking",
        f"quiesce:{label}",
        db_path=db_path,
    )
    return run.id, task


def _install_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setattr(offline_guard, "require_remote_chat", lambda _: None)
    install_completion_backend(monkeypatch, provider)


async def _complete(model: str = _MODEL) -> Any:
    return await complete_request(
        {"model": model, "messages": [{"role": "user", "content": "continue"}]},
        model,
        byok=False,
        timeout_seconds=5,
    )


def _run_once_in_thread(worker_id: str, db_path: str) -> bool:
    return asyncio.run(task_worker.run_once(worker_id, db_path=db_path))


def _run_pool_in_thread(run_id: str, db_path: str) -> None:
    asyncio.run(
        task_worker.run_run_worker_pool(
            run_id,
            "quiesce-pool",
            worker_count=1,
            policy=task_worker.WorkerPolicy(db_path=db_path),
        )
    )


async def test_quiesce_waits_for_accepted_task_calls_and_commit(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, task = _queued_task(isolated_db, "multi-call")
    first_entered = threading.Event()
    first_release = threading.Event()
    second_entered = threading.Event()
    second_release = threading.Event()
    provider_calls: list[int] = []

    async def provider(**_kwargs: Any) -> Any:
        provider_calls.append(len(provider_calls) + 1)
        if len(provider_calls) == 1:
            first_entered.set()
            assert await asyncio.to_thread(first_release.wait, 5)
        else:
            second_entered.set()
            assert await asyncio.to_thread(second_release.wait, 5)
        return completion_response("accepted")

    async def dispatch(_task: ScientificTask, *, db_path: str | None) -> dict[str, int]:
        await _complete()
        await _complete()
        return {"provider_calls": 2}

    _install_provider(monkeypatch, provider)
    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", dispatch)
    worker = asyncio.create_task(
        asyncio.to_thread(_run_once_in_thread, "multi-call-worker", isolated_db)
    )
    operator = make_operator_client()
    try:
        assert await asyncio.to_thread(first_entered.wait, 5)
        state = operator.post(_QUIESCE).json()
        assert state["ready"] is False
        assert state["active_operations"] == 1
        first_release.set()
        assert await asyncio.to_thread(second_entered.wait, 5)
        assert operator.get(_QUIESCE).json()["ready"] is False
        second_release.set()
        assert await asyncio.wait_for(worker, timeout=10) is True
    finally:
        first_release.set()
        second_release.set()
        if not worker.done():
            await asyncio.gather(worker, return_exceptions=True)

    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None and saved.status == "completed"
    assert saved.result == {"provider_calls": 2}
    assert provider_calls == [1, 2]
    assert operator.get(_QUIESCE).json()["ready"] is True
    assert runs.get_run(run_id, db_path=isolated_db) is not None


async def test_quiesce_counts_a_worker_already_inside_task_claim(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, task = _queued_task(isolated_db, "claim-race")
    claim_entered = threading.Event()
    release_claim = threading.Event()
    original_claim = tasks.claim_task

    def blocked_claim(*args: Any, **kwargs: Any) -> ScientificTask | None:
        # Hold the real claim seam after operation admission to force the cutover race.
        claim_entered.set()
        assert release_claim.wait(5)
        return original_claim(*args, **kwargs)

    async def dispatch(_task: ScientificTask, *, db_path: str | None) -> dict[str, bool]:
        return {"claimed": True}

    monkeypatch.setattr(tasks, "claim_task", blocked_claim)
    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", dispatch)
    worker = asyncio.create_task(
        asyncio.to_thread(_run_once_in_thread, "claim-worker", isolated_db)
    )
    operator = make_operator_client()
    try:
        assert await asyncio.to_thread(claim_entered.wait, 5)
        state = operator.post(_QUIESCE).json()
        assert state["ready"] is False
        assert state["active_operations"] == 1
        release_claim.set()
        assert await asyncio.wait_for(worker, timeout=10) is True
    finally:
        release_claim.set()
        if not worker.done():
            await asyncio.gather(worker, return_exceptions=True)

    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None and saved.status == "completed"
    assert operator.get(_QUIESCE).json()["ready"] is True


async def test_quiesce_waits_for_task_persistence_after_provider_answer(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, task = _queued_task(isolated_db, "delayed-persistence")
    provider_returned = threading.Event()
    persistence_entered = threading.Event()
    release_persistence = threading.Event()

    async def provider(**_kwargs: Any) -> Any:
        provider_returned.set()
        return completion_response("answered")

    async def dispatch(_task: ScientificTask, *, db_path: str | None) -> dict[str, bool]:
        await _complete()
        return {"answered": True}

    original_record = task_worker._record_success

    def delayed_record(*args: Any, **kwargs: Any) -> None:
        # A provider answer alone is not safe to redeploy before its durable commit.
        persistence_entered.set()
        assert release_persistence.wait(5)
        original_record(*args, **kwargs)

    _install_provider(monkeypatch, provider)
    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", dispatch)
    monkeypatch.setattr(task_worker, "_record_success", delayed_record)
    worker = asyncio.create_task(
        asyncio.to_thread(_run_once_in_thread, "persistence-worker", isolated_db)
    )
    operator = make_operator_client()
    try:
        assert await asyncio.to_thread(persistence_entered.wait, 5)
        assert provider_returned.is_set()
        state = operator.post(_QUIESCE).json()
        assert state["ready"] is False
        assert state["active_operations"] == 1
        assert state["unanswered_calls"] == 0
        release_persistence.set()
        assert await asyncio.wait_for(worker, timeout=10) is True
    finally:
        release_persistence.set()
        if not worker.done():
            await asyncio.gather(worker, return_exceptions=True)

    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None and saved.status == "completed"
    assert operator.get(_QUIESCE).json()["ready"] is True


async def test_worker_after_ready_does_not_claim_or_consume_an_attempt(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, task = _queued_task(isolated_db, "after-ready")
    dispatches: list[str] = []

    async def dispatch(task: ScientificTask, *, db_path: str | None) -> dict[str, bool]:
        dispatches.append(task.id)
        return {"dispatched": True}

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", dispatch)
    operator = make_operator_client()
    assert operator.post(_QUIESCE).json()["ready"] is True

    assert await task_worker.run_once("late-worker", db_path=isolated_db) is False

    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert saved is not None
    assert saved.status == "queued"
    assert saved.attempt == 0
    assert dispatches == []


async def test_unknown_paid_call_keeps_quiesce_closed_and_spend_unsettled(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, task = _queued_task(isolated_db, "unknown-paid")
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "1")
    monkeypatch.setenv("LLM_AZURE_ENABLED", "true")
    monkeypatch.setenv("LLM_AZURE_EXPIRES_AT", "2099-01-04T00:00:00+00:00")
    monkeypatch.setenv("LLM_USD_TO_EUR", "0.88")
    record_azure_allowance(isolated_db)
    provider_calls = 0

    async def provider(**_kwargs: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise LLMTimeoutError("provider did not answer")

    async def dispatch(_task: ScientificTask, *, db_path: str | None) -> dict[str, bool]:
        await _complete(LUNA)
        return {"unexpected_answer": True}

    _install_provider(monkeypatch, provider)
    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", dispatch)
    assert await task_worker.run_once("unknown-worker", db_path=isolated_db) is True

    operator = make_operator_client()
    state = operator.post(_QUIESCE).json()
    assert state["ready"] is False
    assert state["active_operations"] == 0
    assert state["unanswered_calls"] == 1
    with connect(isolated_db) as conn:
        spend = conn.execute("SELECT charged_microeur, settled FROM llm_spend").fetchone()
        run = conn.execute("SELECT status, error FROM runs WHERE id=?", (run_id,)).fetchone()
    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert spend is not None and spend["charged_microeur"] > 0 and spend["settled"] == 0
    assert run is not None and run["status"] == RunStatus.FAILED.value
    assert run["error"] is not None and "may have accepted" in run["error"].lower()
    assert saved is not None and saved.status == "failed" and saved.attempt == 1
    assert await task_worker.run_once("replay-worker", db_path=isolated_db) is False
    assert provider_calls == 1


async def test_quiesce_waits_for_idle_pool_dead_lease_cleanup(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, task = _queued_task(isolated_db, "pool-cleanup")
    mark_task_leased(
        task.id,
        isolated_db,
        owner="previous-pool",
        expires_at=time.time() - 1,
        spend_budget=True,
    )
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    original_cleanup = tasks_lifecycle.abandon_dead_leases

    def blocked_cleanup(run_id: str, *, db_path: str | None = None) -> int:
        cleanup_entered.set()
        assert release_cleanup.wait(5)
        return original_cleanup(run_id, db_path=db_path)

    monkeypatch.setattr(tasks_lifecycle, "abandon_dead_leases", blocked_cleanup)
    pool = asyncio.create_task(asyncio.to_thread(_run_pool_in_thread, run_id, isolated_db))
    operator = make_operator_client()
    try:
        assert await asyncio.to_thread(cleanup_entered.wait, 5)
        state = operator.post(_QUIESCE).json()
        assert state["ready"] is False
        assert state["active_operations"] == 1
        release_cleanup.set()
        await asyncio.wait_for(pool, timeout=10)
    finally:
        release_cleanup.set()
        if not pool.done():
            await asyncio.gather(pool, return_exceptions=True)

    settled = tasks.get_task(task.id, db_path=isolated_db)
    assert settled is not None and settled.status == "failed"
    assert operator.get(_QUIESCE).json()["ready"] is True


async def test_pool_started_after_ready_does_not_cleanup_dead_leases(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, task = _queued_task(isolated_db, "late-pool-cleanup")
    mark_task_leased(
        task.id,
        isolated_db,
        owner="previous-pool",
        expires_at=time.time() - 1,
        spend_budget=True,
    )
    cleanup_calls: list[str] = []
    original_cleanup = tasks_lifecycle.abandon_dead_leases

    def observed_cleanup(run_id: str, *, db_path: str | None = None) -> int:
        cleanup_calls.append(run_id)
        return original_cleanup(run_id, db_path=db_path)

    monkeypatch.setattr(tasks_lifecycle, "abandon_dead_leases", observed_cleanup)
    operator = make_operator_client()
    assert operator.post(_QUIESCE).json()["ready"] is True

    await asyncio.to_thread(_run_pool_in_thread, run_id, isolated_db)

    saved = tasks.get_task(task.id, db_path=isolated_db)
    assert cleanup_calls == []
    assert saved is not None and saved.status == "leased"
