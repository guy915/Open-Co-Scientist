from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from co_scientist.core.config import settings
from co_scientist.domains.chat.repository.messages import NewMessage, append_message
from co_scientist.main import app
from co_scientist.orchestration.engine_tasks.inputs import (
    enqueue_scientist_continuation,
    reopen_for_pending_scientist_input,
)
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.checkpoints import NewCheckpoint, save_checkpoint
from co_scientist.platform.db.launch_control import read_control, write_control
from co_scientist.platform.db.models import RunStatus
from fastapi.testclient import TestClient

from tests._client import create_run, make_client, make_operator_client


def _completed(client: TestClient) -> str:
    run_id = create_run(client, "Study molecular folding").json()["id"]
    assert isinstance(run_id, str)
    save_checkpoint(run_id, NewCheckpoint("finalize", 1, 0, {"iteration": 2, "api_calls": 7}))
    runs.update_run_status(run_id, RunStatus.COMPLETED)
    return run_id


def _limit(monkeypatch: pytest.MonkeyPatch, name: str, value: int) -> None:
    if name in type(settings).model_fields:
        monkeypatch.setattr(settings, name, value)
    else:
        monkeypatch.setattr(type(settings), name, value, raising=False)


def test_completed_steering_obeys_global_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, "max_concurrent_runs", 1)
    client = make_client()
    completed = _completed(client)
    active = create_run(client, "Already running", headers={"X-Client-ID": "other"}).json()["id"]
    runs.update_run_status(active, RunStatus.RUNNING)
    response = client.post(
        f"/api/runs/{completed}/messages", json={"content": "Consider another mechanism"}
    )
    assert response.status_code == 409
    run = runs.get_run(completed)
    assert run is not None and run.status == "completed"
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scientific_tasks WHERE run_id=?", (completed,)
            ).fetchone()[0]
            == 0
        )


def test_concurrent_reopens_cannot_overdraw_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, "max_concurrent_runs", 1)
    clients = [TestClient(app, headers={"X-Client-ID": owner}) for owner in ("one", "two")]
    targets = [_completed(client) for client in clients]

    def reopen(index: int) -> int:
        return (
            clients[index]
            .post(
                f"/api/runs/{targets[index]}/messages",
                json={"content": "Consider another mechanism"},
            )
            .status_code
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reopen, (0, 1))) == [200, 409]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='queued'").fetchone()[0] == 1


def test_continuation_daily_allowance_survives_completed_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limit(monkeypatch, "continuations_per_run_per_day", 1)
    client = make_client()
    run_id = _completed(client)
    assert (
        client.post(
            f"/api/runs/{run_id}/messages", json={"content": "First continuation"}
        ).status_code
        == 200
    )
    runs.update_run_status(run_id, RunStatus.COMPLETED)
    db._initialized.clear()
    assert (
        client.post(
            f"/api/runs/{run_id}/messages", json={"content": "Another continuation"}
        ).status_code
        == 429
    )
    run = runs.get_run(run_id)
    assert run is not None and run.status == "completed"


def test_duplicate_input_cannot_reopen_or_duplicate_lifecycle() -> None:
    run_id = _completed(make_client())
    message = append_message(NewMessage(run_id, "user", "Consider another mechanism", "steering"))
    task = enqueue_scientist_continuation(run_id, message.id)
    assert task is not None
    runs.update_run_status(run_id, RunStatus.COMPLETED)
    repeated = enqueue_scientist_continuation(run_id, message.id)
    assert repeated is None or repeated.id == task.id
    run = runs.get_run(run_id)
    assert run is not None and run.status == "completed"
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id=? "
                "AND json_extract(payload_json,'$.event')='reopened_for_scientist_input'",
                (run_id,),
            ).fetchone()[0]
            == 1
        )


def test_finalize_pending_input_cannot_bypass_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, "max_concurrent_runs", 1)
    client = make_client()
    run_id = _completed(client)
    active = create_run(client, "Active work").json()["id"]
    runs.update_run_status(active, RunStatus.RUNNING)
    append_message(NewMessage(run_id, "user", "Late steering", "steering"))
    assert reopen_for_pending_scientist_input(run_id) is None
    run = runs.get_run(run_id)
    assert run is not None and run.status == "completed"


def test_pause_does_not_replay_finalize_with_already_admitted_pending_input() -> None:
    run_id = _completed(make_client())
    append_message(NewMessage(run_id, "user", "Late steering", "steering"))
    with db.transaction() as conn:
        write_control(
            conn,
            paused=True,
            drain=False,
            message="Synthetic pause",
            resumes_at=None,
            expected_revision=read_control(conn=conn).revision,
        )
    assert reopen_for_pending_scientist_input(run_id) is None
    run = runs.get_run(run_id)
    assert run is not None and run.status == "completed"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scientific_tasks").fetchone()[0] == 0
        assert (
            conn.execute("SELECT applied FROM messages WHERE run_id=?", (run_id,)).fetchone()[0]
            == 0
        )
    response = make_operator_client().put(
        "/api/launch-control", json={"paused": False, "expected_revision": read_control().revision}
    )
    assert response.status_code == 200
    resumed = runs.get_run(run_id)
    assert resumed is not None and resumed.status == "queued"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scientific_tasks").fetchone()[0] == 1


def test_resuming_pending_input_preserves_capacity_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    from co_scientist.orchestration.engine_tasks.inputs import resume_pending_scientist_inputs

    _limit(monkeypatch, "max_concurrent_runs", 1)
    client = make_client()
    run_id = _completed(client)
    active = create_run(client, "Active work").json()["id"]
    runs.update_run_status(active, RunStatus.RUNNING)
    append_message(NewMessage(run_id, "user", "Late steering", "steering"))
    resume_pending_scientist_inputs()
    run = runs.get_run(run_id)
    assert run is not None and run.status == "completed"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scientific_tasks").fetchone()[0] == 0


@pytest.mark.parametrize(
    "setting",
    ["continuations_per_client_per_day", "continuations_per_host_per_day", "continuations_per_day"],
)
def test_continuation_allowances_span_distinct_runs(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    _limit(monkeypatch, setting, 1)
    client = make_client()
    first = _completed(client)
    second = _completed(client)
    assert (
        client.post(
            f"/api/runs/{first}/messages", json={"content": "First continuation"}
        ).status_code
        == 200
    )
    runs.update_run_status(first, RunStatus.COMPLETED)
    assert (
        client.post(
            f"/api/runs/{second}/messages", json={"content": "Another continuation"}
        ).status_code
        == 429
    )


def test_continuations_charge_free_allowance_and_ignore_unstored_byok_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limit(monkeypatch, "free_runs_per_day", 2)
    client = make_client()
    run_id = _completed(client)
    with db.connect() as conn:
        conn.execute("UPDATE runs SET llm_backend='real' WHERE id=?", (run_id,))
        conn.execute(
            "INSERT INTO free_run_usage VALUES (?,?,?)",
            (run_id, "pytest-default-client", db.current_time()),
        )
    assert (
        client.post(
            f"/api/runs/{run_id}/messages", json={"content": "First continuation"}
        ).status_code
        == 200
    )
    runs.update_run_status(run_id, RunStatus.COMPLETED)
    denied = client.post(
        f"/api/runs/{run_id}/messages",
        json={"content": "Another continuation"},
        headers={"X-LLM-API-Key": "synthetic-not-a-provider-key", "X-LLM-Provider": "openrouter"},
    )
    assert denied.status_code == 429
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM free_run_usage").fetchone()[0] == 2


def test_failed_enqueue_rolls_back_admission_status_and_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.orchestration.repository import tasks

    client = make_client()
    run_id = _completed(client)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic enqueue failure")

    monkeypatch.setattr(tasks, "enqueue_task", fail)
    with pytest.raises(RuntimeError, match="synthetic enqueue failure"):
        client.post(f"/api/runs/{run_id}/messages", json={"content": "Consider another mechanism"})
    run = runs.get_run(run_id)
    assert run is not None and run.status == "completed"
    with db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM messages WHERE run_id=?", (run_id,)).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM run_admissions WHERE run_id LIKE 'continuation:%'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id=? AND "
                "json_extract(payload_json,'$.event')='reopened_for_scientist_input'",
                (run_id,),
            ).fetchone()[0]
            == 0
        )


def test_continuation_preserves_accumulated_checkpoint_usage() -> None:
    from co_scientist.platform.db.checkpoints import get_latest_checkpoint

    client = make_client()
    run_id = _completed(client)
    assert (
        client.post(
            f"/api/runs/{run_id}/messages", json={"content": "Consider another mechanism"}
        ).status_code
        == 200
    )
    checkpoint = get_latest_checkpoint(run_id)
    assert checkpoint is not None and checkpoint["state"] == {"iteration": 2, "api_calls": 7}
