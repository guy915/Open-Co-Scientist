from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from co_scientist.api import diagnostics
from co_scientist.api.diagnostics import (
    HealthCheck,
)
from co_scientist.api.version import API_VERSION
from co_scientist.core.config import settings
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository.tasks import queue_health_snapshot
from co_scientist.orchestration.repository.tasks_lifecycle import QueueHealthSnapshot
from co_scientist.platform import db
from co_scientist.platform.db import checkpoints, runs
from co_scientist.platform.db.models import DEMO_CLIENT_ID, RunStatus
from fastapi.testclient import TestClient

from tests._client import make_client as _client
from tests._client import make_operator_client
from tests._client import make_operator_client as _operator_client
from tests._store_helpers import enqueue_task, seed_checkpoint, seed_run


def test_health_ok() -> None:
    client = _client()
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "model_name" in data
    assert data["version"] == API_VERSION
    assert data["provider"] == "engine"
    assert data["checks"]["store"]["ok"] is True
    assert set(data["checks"]) == {"store", "engine", "queue", "disk"}
    assert data["checks"]["queue"]["ok"] is True
    assert data["checks"]["disk"]["ok"] is True


@pytest.mark.parametrize("operator", [True, False])
def test_health_is_unhealthy_when_the_store_is_unreachable_and_hides_detail(
    monkeypatch: pytest.MonkeyPatch, operator: bool
) -> None:
    monkeypatch.setattr(
        diagnostics,
        "check_store",
        lambda db_path=None: HealthCheck(ok=False, detail="disk on fire"),
    )

    res = (make_operator_client() if operator else _client()).get("/health")

    data = res.json()
    assert res.status_code == 503
    assert data["checks"]["store"]["ok"] is False
    if operator:
        assert data["status"] == "unhealthy"
        assert data["checks"]["store"]["detail"] == "disk on fire"
    else:
        assert data["checks"]["store"]["detail"] is None
        assert data["model_name"] is None


def test_status_reports_the_offline_backend_and_probes_only_to_operators() -> None:
    data = _client().get("/status").json()
    assert data["provider"] == "engine"
    assert data["llm_backend"] == "offline"
    assert data["probes"] is None
    operator = make_operator_client().get("/status").json()
    assert set(operator["probes"]) == {"mcp", "pubmed", "web_search"}


@pytest.mark.parametrize("operator", [True, False])
def test_api_docs_are_not_served(operator: bool) -> None:
    client = make_operator_client() if operator else _client()

    assert "docs" not in client.get("/").json()
    for path in ("/docs", "/redoc", "/openapi.json", "/metrics", "/config"):
        assert client.get(path).status_code == 404, path


def _seed_interrupted_engine_run(isolated_db: str) -> str:
    interrupted = seed_run("interrupted goal", profile="default", db_path=isolated_db)
    runs.update_run_status(interrupted.id, RunStatus.RUNNING, db_path=isolated_db)
    seed_checkpoint(
        interrupted.id,
        {"provider": "engine", "state": {"hypotheses": []}},
        stage="engine_task:test",
        db_path=isolated_db,
    )
    return interrupted.id


def test_lifespan_reconciles_interrupted_runs_and_seeds_demo_data(
    isolated_db: str,
) -> None:
    # TestClient only enters lifespan as a context manager; construction alone
    # does not exercise startup.
    import co_scientist.main as main_module

    interrupted = seed_run(
        "interrupted goal",
        profile="default",
        provider="mock",
        db_path=isolated_db,
    )
    runs.update_run_status(interrupted.id, RunStatus.RUNNING, db_path=isolated_db)

    with TestClient(main_module.app) as client:
        res = client.get("/health")
        assert res.status_code == 200

    reconciled = runs.get_run(interrupted.id, db_path=isolated_db)
    assert reconciled is not None
    assert reconciled.status == RunStatus.FAILED.value
    assert reconciled.error and "restart" in reconciled.error

    demo_runs = runs.list_runs(client_id=DEMO_CLIENT_ID, db_path=isolated_db)
    assert len(demo_runs) == 3


def test_status_redacts_operator_fields_from_non_operators(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MCP hostnames and credential state are operator internals; public
    # availability remains usable.

    body = _client().get("/status").json()

    assert body["mcp_server_url"] is None
    assert body["has_provider_key"] is None
    assert body["byok_enabled"] is None
    assert body["engine_importable"] is None
    assert body["supervisor_model_name"] is None
    assert body["enabled_tools"] is None
    assert body["probes"] is None
    assert isinstance(body["connectors"], list)
    assert body["provider"] == "engine"
    assert "llm_backend" in body
    assert "model_name" in body


def test_startup_prunes_checkpoints_but_never_vacuums(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # VACUUM waits for exclusive access while log writes continue; pruning must
    # never block serving writers.
    import co_scientist.main as main_module

    pruned = threading.Event()

    def _prune(*args: object, **kwargs: object) -> int:
        pruned.set()
        return 0

    monkeypatch.setattr(checkpoints, "prune_superseded_checkpoints", _prune)

    with TestClient(main_module.app) as client:
        assert client.get("/health").status_code == 200

    assert pruned.is_set(), "startup should still reclaim checkpoint rows"


def test_startup_does_not_block_on_run_recovery(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Run recovery after binding traffic; provider work before lifespan yield
    # causes healthcheck restart spirals.
    import co_scientist.api.runs as runs_module
    import co_scientist.main as main_module

    resumed = threading.Event()

    async def slow_resume(run_ids: list[str]) -> None:
        resumed.set()
        await asyncio.sleep(30)

    async def _no_seed(db_path: str | None = None) -> None:
        # Stub demo compute so the timing assertion measures recovery scheduling
        # rather than seed work.
        return None

    monkeypatch.setattr(runs_module, "resume_interrupted_runs", slow_resume)
    monkeypatch.setattr(main_module, "seed_demo_runs", _no_seed)
    _seed_interrupted_engine_run(isolated_db)

    started = time.monotonic()
    with TestClient(main_module.app) as client:
        startup_seconds = time.monotonic() - started
        assert client.get("/health").status_code == 200
    assert resumed.is_set()
    assert startup_seconds < 10


# Queue/disk degradation returns HTTP 200 so liveness probes cannot kill
# productive runs.


def _health_running_run(db_path: str, goal: str = "queue health goal") -> str:
    run = seed_run(goal)
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=db_path)
    return run.id


def _health_enqueue(run_id: str, key: str, db_path: str, **kwargs: Any) -> str:
    task = enqueue_task(run_id, "engine.node.ranking", key, **kwargs, db_path=db_path)
    return task.id


def _expire_lease(task_id: str, db_path: str) -> None:
    # Expire the lease without changing attempts to model a worker that died
    # without failing its task.
    with db.connect(db_path) as conn:
        conn.execute(
            "UPDATE scientific_tasks SET lease_expires_at=0 WHERE id=?",
            (task_id,),
        )
        conn.commit()


def _snapshot_with_lease(
    db_path: str, *, max_attempts: int = 3, expired: bool = False
) -> tuple[str, QueueHealthSnapshot]:
    run_id = _health_running_run(db_path)
    task_id = _health_enqueue(run_id, "leased", db_path, max_attempts=max_attempts)
    leased = store.claim_task("w1", run_id=run_id, db_path=db_path)
    assert leased is not None and leased.id == task_id
    if expired:
        _expire_lease(task_id, db_path)
    return run_id, queue_health_snapshot(db_path=db_path)


def test_orphaned_exhausted_lease_leaves_run_stalled(isolated_db: str) -> None:
    # A dead exhausted lease has no owner to fail it; health must expose the
    # stranded running state.
    run_id, snapshot = _snapshot_with_lease(isolated_db, max_attempts=1, expired=True)

    assert snapshot.stalled_run_ids == (run_id,)
    assert snapshot.rescuable_leases == 0
    run = runs.get_run(run_id, db_path=isolated_db)
    assert run is not None and run.status == "running"


def test_health_degrades_at_200_when_a_run_is_stalled(
    isolated_db: str,
) -> None:
    run_id, _ = _snapshot_with_lease(isolated_db, max_attempts=1, expired=True)

    res = _client().get("/health")

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "degraded"
    assert data["checks"]["queue"]["ok"] is False
    operator = _operator_client().get("/health").json()
    assert run_id in (operator["checks"]["queue"]["detail"] or "")
    assert data["checks"]["queue"]["detail"] is None


def test_health_degrades_at_200_when_disk_is_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "health_check_min_free_disk_bytes", 10**18)

    res = _client().get("/health")

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "degraded"
    assert data["checks"]["disk"]["ok"] is False
