from __future__ import annotations

import concurrent.futures
import dataclasses
import json
from typing import Any

import co_scientist.orchestration.repository.tasks_lifecycle as store_tasks_attempts
import pytest
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.core.exceptions import LLMCallBudgetExceededError, LLMTimeoutError
from co_scientist.domains.access import credentials
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.repository import events as store_events
from co_scientist.orchestration.repository import runs_views as views
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository import tasks as store_tasks
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform import db as store_db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import (
    UNKNOWN_PROVIDER_OUTCOME_ERROR,
    RunStatus,
    ScientificTask,
)
from co_scientist.platform.db.runs import RunCreateOptions

from tests._client import DEFAULT_TEST_CLIENT_ID, make_client
from tests._client import create_run as _create_run
from tests._store_helpers import enqueue_task, seed_run


# The zero-cost stamp enforces exact zero price; lost leases are retry-safe
# only without caller credentials.
def _stamped_run_with_expired_lease(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    run = seed_run(
        "Zero-cost lease loss",
        profile="express",
        config={"zero_cost_admission": True},
        options=RunCreateOptions(client_id=DEFAULT_TEST_CLIENT_ID),
    )
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    task = enqueue_task(
        run.id,
        "engine.fanout.verification.item",
        "verification:seed",
        db_path=isolated_db,
    )
    leased = store.claim_task(
        "restarted-worker", run_id=run.id, lease_seconds=1, db_path=isolated_db
    )
    assert leased is not None
    now = store_db.current_time()
    monkeypatch.setattr("co_scientist.platform.db.time.time", lambda: now + 2)
    return run.id, task.id


@pytest.mark.asyncio
async def test_expired_provably_free_lease_is_retried(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    replayed: list[str] = []

    async def _replay(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        replayed.append(task.task_type)
        return {"replayed": True}

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", _replay)
    run_id, task_id = _stamped_run_with_expired_lease(isolated_db, monkeypatch)

    assert await task_worker.run_once("new-worker", db_path=isolated_db)

    assert replayed == ["engine.fanout.verification.item"]
    task = store.get_task(task_id, db_path=isolated_db)
    assert task is not None and task.status == "completed"
    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status != RunStatus.FAILED


@pytest.mark.asyncio
async def test_expired_provably_free_lease_with_byok_still_fails_closed(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "byok_encryption_key", "synthetic-zero-cost-lease-secret")
    replayed: list[str] = []

    async def _must_not_call(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        replayed.append(task.task_type)
        return {}

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", _must_not_call)
    run_id, task_id = _stamped_run_with_expired_lease(isolated_db, monkeypatch)
    credentials.store_run_credential(
        run_id,
        DEFAULT_TEST_CLIENT_ID,
        byok_scope.ByokCredential(
            provider="deepseek",
            api_key="sk-synthetic-zero-cost-lease-12345",
            model="deepseek/deepseek-v4-flash",
        ),
        db_path=isolated_db,
    )

    assert not await task_worker.run_once("new-worker", db_path=isolated_db)

    assert replayed == []
    task = store.get_task(task_id, db_path=isolated_db)
    assert task is not None and task.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{}, {"zero_cost_admission": "yes"}])
async def test_expired_standard_lease_without_the_stamp_waits_for_explicit_resume(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]
) -> None:
    run = seed_run(
        "Unproven lease loss",
        config=config,
        options=RunCreateOptions(client_id=DEFAULT_TEST_CLIENT_ID),
    )
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    task = enqueue_task(run.id, "engine.node.generate", "generate:seed", db_path=isolated_db)
    assert store.claim_task("restarted-worker", run_id=run.id, lease_seconds=1, db_path=isolated_db)
    now = store_db.current_time()
    monkeypatch.setattr("co_scientist.platform.db.time.time", lambda: now + 2)

    assert not await task_worker.run_once("new-worker", db_path=isolated_db)

    paused = store.get_task(task.id, db_path=isolated_db)
    assert paused is not None and paused.status == "paused"
    assert paused.error == UNKNOWN_PROVIDER_OUTCOME_ERROR


@pytest.mark.asyncio
async def test_a_zero_cost_stamped_task_admits_only_zero_price_requests(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.llm.admission import free_policy

    run = seed_run(
        "Stamped run",
        config={"zero_cost_admission": True},
        options=RunCreateOptions(client_id=DEFAULT_TEST_CLIENT_ID),
    )
    task = enqueue_task(run.id, "engine.node.generate", "generate:scope", db_path=isolated_db)
    paid = {"model": "openrouter/provider/paid-model"}
    observed: list[bool] = []

    async def _probe(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        observed.append(free_policy._requires_free(paid, False))
        return {}

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", _probe)

    await engine_tasks.execute_engine_task(task, db_path=isolated_db)

    assert observed == [True]
    assert free_policy._requires_free(paid, False) is False


# Each failed attempt needs bounded history; overwriting one error loses
# distinct failure diagnoses.


def _history_running_run(db_path: str, goal: str = "attempts goal") -> str:
    run = seed_run(goal)
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=db_path)
    return run.id


def _history_enqueue(run_id: str, key: str, db_path: str, *, max_attempts: int = 3) -> str:
    task = enqueue_task(
        run_id,
        "engine.node.ranking",
        key,
        max_attempts=max_attempts,
        db_path=db_path,
    )
    return task.id


def test_lease_renewal_does_not_move_recorded_start_time(
    isolated_db: str,
) -> None:
    # Heartbeats update lease timestamps, but attempt history must retain the
    # original claim time.
    run_id = _history_running_run(isolated_db)
    task_id = _history_enqueue(run_id, "k", isolated_db, max_attempts=3)

    leased = store.claim_task("w1", run_id=run_id, db_path=isolated_db)
    assert leased is not None
    claim_time = leased.updated_at

    assert lifecycle.renew_task_lease(leased.id, "w1", 60.0, db_path=isolated_db)
    assert store.fail_task(leased.id, "w1", "timed out", db_path=isolated_db)

    saved = store.get_task(task_id, db_path=isolated_db)
    assert saved is not None
    assert saved.attempts[0]["started_at"] == claim_time


def test_attempts_history_is_capped(isolated_db: str) -> None:
    run_id = _history_running_run(isolated_db)
    over_cap = store_tasks_attempts._MAX_STORED_ATTEMPTS + 3
    task_id = _history_enqueue(run_id, "k", isolated_db, max_attempts=over_cap + 1)

    for i in range(over_cap):
        leased = store.claim_task("w", run_id=run_id, db_path=isolated_db)
        assert leased is not None
        assert store.fail_task(leased.id, "w", f"failure {i}", db_path=isolated_db)

    saved = store.get_task(task_id, db_path=isolated_db)
    assert saved is not None
    assert len(saved.attempts) == store_tasks_attempts._MAX_STORED_ATTEMPTS
    assert saved.attempts[-1]["error"] == f"failure {over_cap - 1}"
    first_kept = over_cap - store_tasks_attempts._MAX_STORED_ATTEMPTS
    assert saved.attempts[0]["error"] == f"failure {first_kept}"
    assert [a["attempt"] for a in saved.attempts] == list(range(first_kept + 1, over_cap + 1))


def test_failed_attempt_write_is_transactional_with_settlement(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _history_running_run(isolated_db)
    task_id = _history_enqueue(run_id, "k", isolated_db, max_attempts=1)
    leased = store.claim_task("w1", run_id=run_id, db_path=isolated_db)
    assert leased is not None

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("settlement exploded")

    monkeypatch.setattr(store_tasks, "_settle_run_for_failed_task", _boom)

    with pytest.raises(RuntimeError, match="settlement exploded"):
        store.fail_task(leased.id, "w1", "boom", retryable=False, db_path=isolated_db)

    saved = store.get_task(task_id, db_path=isolated_db)
    assert saved is not None
    assert saved.status == "leased"
    assert saved.attempts == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "attempts", "expected_kind", "expected_message"),
    [
        pytest.param(
            LLMCallBudgetExceededError(count=251, ceiling=250),
            1,
            "llm_call_budget_exceeded",
            "251 provider requests against a budget of 250",
            id="budget",
        ),
        pytest.param(
            LLMTimeoutError("provider timed out"),
            1,
            "llm_timeout_unknown",
            UNKNOWN_PROVIDER_OUTCOME_ERROR,
            id="timeout",
        ),
        pytest.param(
            RuntimeError("LLM-call ceiling exceeded: 251 provider requests"),
            3,
            None,
            "LLM-call ceiling exceeded: 251 provider requests",
            id="near-miss-is-unclassified",
        ),
    ],
)
async def test_owned_run_api_classifies_only_exact_terminal_failure_types(
    manual_worker: None,
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    attempts: int,
    expected_kind: str | None,
    expected_message: str,
) -> None:

    async def _raise_known_or_near_miss(
        _task: ScientificTask, *, db_path: str | None = None
    ) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(engine_tasks, "execute_engine_task", _raise_known_or_near_miss)

    with make_client() as client:
        created = _create_run(client, "typed failure goal")
        run_id = created.json()["id"]
        assert client.post(f"/api/runs/{run_id}/start", json={}).status_code == 200
        for _ in range(attempts):
            assert await task_worker.run_once("worker", db_path=isolated_db)

    with make_client() as reopened:
        body = reopened.get(f"/api/runs/{run_id}").json()

    assert body["status"] == "failed"
    assert body["failure_kind"] == expected_kind
    assert expected_message in body["error"]


@pytest.mark.asyncio
async def test_run_failure_kind_comes_from_task_that_settles_run(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:

    async def _fail_tasks(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        if task.task_type == "engine.bootstrap":
            raise LLMCallBudgetExceededError(count=251, ceiling=250)
        raise RuntimeError("LLM-call ceiling exceeded: near miss")

    monkeypatch.setattr(engine_tasks, "execute_engine_task", _fail_tasks)

    with make_client() as client:
        created = _create_run(client, "sibling failure goal")
        run_id = created.json()["id"]
        assert client.post(f"/api/runs/{run_id}/start", json={}).status_code == 200
        enqueue_task(
            run_id,
            "engine.node.generate",
            "sibling",
            max_attempts=1,
            db_path=isolated_db,
        )

        assert await task_worker.run_once("worker", run_id=run_id, db_path=isolated_db)
        active = client.get(f"/api/runs/{run_id}").json()
        assert active["status"] == "queued"
        assert active["failure_kind"] is None

        assert await task_worker.run_once("worker", run_id=run_id, db_path=isolated_db)
        failed = client.get(f"/api/runs/{run_id}").json()

    assert failed["status"] == "failed"
    assert failed["failure_kind"] is None


_BYOK_SECRET = "synthetic-byok-encryption-secret"
_BYOK_KEY = "sk-synthetic-echo-redaction-67890"
_DIAGNOSTIC = "provider diagnostic preserved"


def _redaction_parse_sse(text: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :]) for line in text.splitlines() if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_durable_byok_failure_redacts_owned_surfaces_after_reopen(
    manual_worker: None, isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "byok_encryption_key", _BYOK_SECRET)

    async def _echo_key(_task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        raise LLMTimeoutError(f"provider echoed {_BYOK_KEY}; {_DIAGNOSTIC}")

    monkeypatch.setattr(engine_tasks, "_dispatch_engine_task", _echo_key)

    with make_client() as client:
        owner = {"X-Client-ID": "failure-owner"}
        other = {"X-Client-ID": "failure-other"}
        created = _create_run(client, "owned synthetic failure", headers=owner)
        assert created.status_code == 200
        run_id = created.json()["id"]
        credentials.store_run_credential(
            run_id,
            "failure-owner",
            byok_scope.ByokCredential(
                provider="deepseek",
                api_key=_BYOK_KEY,
                model="deepseek/deepseek-v4-flash",
            ),
            db_path=isolated_db,
        )
        started = client.post(f"/api/runs/{run_id}/start", headers=owner, json={})
        assert started.status_code == 200
        assert await task_worker.run_once("synthetic-worker", db_path=isolated_db)
        assert not await task_worker.run_once("synthetic-worker", db_path=isolated_db)

    with make_client() as reopened:
        run = reopened.get(f"/api/runs/{run_id}", headers=owner)
        events = reopened.get(f"/api/runs/{run_id}/events", headers=owner)
        other_run = reopened.get(f"/api/runs/{run_id}", headers=other)
        other_events = reopened.get(f"/api/runs/{run_id}/events", headers=other)

    assert run.status_code == 200
    assert events.status_code == 200
    assert other_run.status_code == other_events.status_code == 404
    run_body = run.json()
    with store_db.connect(isolated_db) as conn:
        run_logs = [
            dict(row) for row in conn.execute("SELECT * FROM app_logs WHERE run_id = ?", (run_id,))
        ]
    rows = store.list_tasks(run_id, db_path=isolated_db)
    replayed = _redaction_parse_sse(events.text)
    owned_output = json.dumps(
        [run_body, [dataclasses.asdict(t) for t in rows], replayed, run_logs], sort_keys=True
    )
    assert run_body["status"] == "failed"
    assert run_body["failure_kind"] == "llm_timeout_unknown"
    assert _BYOK_KEY not in owned_output
    assert _DIAGNOSTIC not in owned_output
    assert rows[-1].status == "failed" and len(rows[-1].attempts) == 1
    assert replayed[-1]["type"] == "_terminal"
    failed = [
        event
        for event in replayed
        if event["type"] == "status" and event.get("payload", {}).get("status") == "failed"
    ]
    assert len(failed) == 1
    assert any(row.get("exc_text") == "LLMTimeoutError" for row in run_logs)


# Settle runs transactionally when their last claimable task fails, or SSE never
# closes.


def _settlement_running_run(db_path: str, goal: str = "settlement goal") -> str:
    run = seed_run(goal)
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=db_path)
    return run.id


def _failed_status_events(run_id: str, db_path: str) -> list[dict[str, Any]]:
    return [
        event
        for event in store_events.list_events(run_id, db_path=db_path)
        if event["type"] == "status" and (event.get("payload") or {}).get("status") == "failed"
    ]


def test_exhausted_retry_budget_settles_run(isolated_db: str) -> None:
    run_id = _settlement_running_run(isolated_db)
    task_id = _history_enqueue(run_id, "doomed", isolated_db, max_attempts=2)

    first = store.claim_task("w1", run_id=run_id, db_path=isolated_db)
    assert first is not None and first.id == task_id
    assert store.fail_task(first.id, "w1", "provider timeout", db_path=isolated_db)
    saved = store.get_task(task_id, db_path=isolated_db)
    assert saved is not None and saved.status == "queued"
    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == "running"

    second = store.claim_task("w2", run_id=run_id, db_path=isolated_db)
    assert second is not None and second.attempt == 2
    assert store.fail_task(second.id, "w2", "provider timeout again", db_path=isolated_db)

    saved = store.get_task(task_id, db_path=isolated_db)
    assert saved is not None and saved.status == "failed"
    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None
    assert run.status == "failed"
    assert run.error is not None
    assert "provider timeout again" in run.error
    assert run.completed_at is not None

    failed_events = _failed_status_events(run_id, isolated_db)
    assert len(failed_events) == 1
    assert "provider timeout again" in failed_events[0]["payload"]["error"]


@pytest.mark.parametrize("sibling_leased", [False, True])
def test_a_live_sibling_task_blocks_settlement(isolated_db: str, sibling_leased: bool) -> None:
    run_id = _settlement_running_run(isolated_db)
    doomed = _history_enqueue(run_id, "doomed", isolated_db, max_attempts=1)
    _history_enqueue(run_id, "sibling", isolated_db, max_attempts=1)
    leased = store.claim_task("w1", run_id=run_id, db_path=isolated_db)
    assert leased is not None and leased.id == doomed
    if sibling_leased:
        assert store.claim_task("w2", run_id=run_id, db_path=isolated_db)

    assert store.fail_task(leased.id, "w1", "boom", retryable=False, db_path=isolated_db)

    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == "running"
    assert _failed_status_events(run_id, isolated_db) == []


def test_task_failure_does_not_settle_inactive_run(isolated_db: str) -> None:
    run = seed_run("draft goal")
    task_id = _history_enqueue(run.id, "doomed", isolated_db)
    leased = store.claim_task("w1", run_id=run.id, db_path=isolated_db)
    assert leased is not None and leased.id == task_id

    assert store.fail_task(leased.id, "w1", "boom", retryable=False, db_path=isolated_db)

    saved = runs.get_run(run.id, db_path=isolated_db)
    assert saved is not None and saved.status == "draft"
    assert _failed_status_events(run.id, isolated_db) == []


def test_concurrent_final_failures_settle_exactly_once(
    isolated_db: str,
) -> None:
    run_id = _settlement_running_run(isolated_db)
    task_a = _history_enqueue(run_id, "a", isolated_db, max_attempts=1)
    task_b = _history_enqueue(run_id, "b", isolated_db, max_attempts=1)
    leased_a = store.claim_task("w1", run_id=run_id, db_path=isolated_db)
    leased_b = store.claim_task("w2", run_id=run_id, db_path=isolated_db)
    assert leased_a is not None and leased_a.id == task_a
    assert leased_b is not None and leased_b.id == task_b

    def _fail(task_id: str, worker: str) -> bool:
        return store.fail_task(task_id, worker, "boom", retryable=False, db_path=isolated_db)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            pool.submit(_fail, leased_a.id, "w1"),
            pool.submit(_fail, leased_b.id, "w2"),
        ]
        assert all(future.result() for future in outcomes)

    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == "failed"
    assert len(_failed_status_events(run_id, isolated_db)) == 1


def test_settled_run_is_not_reprocessed_at_startup(isolated_db: str) -> None:
    run_id = _settlement_running_run(isolated_db)
    task_id = _history_enqueue(run_id, "doomed", isolated_db, max_attempts=1)
    leased = store.claim_task("w1", run_id=run_id, db_path=isolated_db)
    assert leased is not None and leased.id == task_id
    assert store.fail_task(leased.id, "w1", "boom", retryable=False, db_path=isolated_db)
    events_before = store_events.list_events(run_id, db_path=isolated_db)

    reconciled = views.reconcile_interrupted_runs(db_path=isolated_db)

    assert run_id not in reconciled["failed"]
    assert run_id not in reconciled["resumable"]
    assert store_events.list_events(run_id, db_path=isolated_db) == events_before


@pytest.mark.asyncio
async def test_unsupported_task_type_fails_permanently_and_settles_run(
    isolated_db: str,
) -> None:
    run_id = _settlement_running_run(isolated_db)
    task = enqueue_task(run_id, "unknown.task", "unknown", max_attempts=3, db_path=isolated_db)

    assert await task_worker.run_once("w1", db_path=isolated_db)

    failed = store.get_task(task.id, db_path=isolated_db)
    assert failed is not None and failed.status == "failed"
    assert failed.attempt < failed.max_attempts, "an unknown type is not retried"
    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None
    assert run.status == "failed"
    assert "unsupported task type" in (run.error or "")
    assert len(_failed_status_events(run_id, isolated_db)) == 1
