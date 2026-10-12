from __future__ import annotations

import asyncio
import contextvars
import json
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from co_scientist.api import interviews as interview_api
from co_scientist.api import launch_admission, launch_control_api
from co_scientist.api.interviews import turns as interview_turns
from co_scientist.api.runs import chat, contrib, crud, lifecycle
from co_scientist.core.config import settings
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.engine_tasks import inputs
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository.tasks_lifecycle import owns_task_lease
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.launch_control import LaunchPausedError, read_control, write_control
from co_scientist.platform.db.models import RunRow, RunStatus, ScientificTask
from fastapi.responses import StreamingResponse

from tests._azure_ledger import record_azure_allowance
from tests._client import DEFAULT_TEST_CLIENT_ID, create_run, make_client, make_operator_client
from tests._process_mode_helpers import FakeProcessMode
from tests._store_helpers import enqueue_task, seed_checkpoint, seed_run


def _set_pause(*, paused: bool = True, drain: bool = False) -> None:
    with db.transaction(durable=True) as conn:
        current = read_control(conn=conn)
        write_control(
            conn,
            paused=paused,
            drain=drain,
            message="Synthetic pause",
            resumes_at=None,
            expected_revision=current.revision,
        )


def test_only_a_configured_operator_token_can_read_or_write_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    for headers in ({}, {"X-Logs-Token": "wrong"}, {"X-Forwarded-For": "127.0.0.1"}):
        assert client.get("/api/launch-control", headers=headers).status_code == 403
        assert (
            client.put(
                "/api/launch-control",
                headers=headers,
                json={"paused": True, "expected_revision": 0},
            ).status_code
            == 403
        )
    operator = make_operator_client()
    assert (
        operator.put(
            "/api/launch-control", json={"paused": True, "expected_revision": 0}
        ).status_code
        == 200
    )
    monkeypatch.setattr(settings, "logs_admin_token", "")
    assert operator.get("/api/launch-control").status_code == 403


def test_control_survives_a_fresh_process_and_never_resumes_on_an_eta(isolated_db: str) -> None:
    _set_pause()
    code = (
        "import json; from dataclasses import asdict; "
        "from co_scientist.platform.db.launch_control import read_control; "
        "print(json.dumps(asdict(read_control())))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.defpath,
            "COSCIENTIST_DB_PATH": isolated_db,
            "COSCIENTIST_TEST_DOUBLE": "deterministic",
            "PYTHON_DOTENV_DISABLED": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
    )
    assert json.loads(result.stdout)["paused"] is True
    with db.transaction() as conn:
        conn.execute("UPDATE launch_control SET resumes_at=1")
    assert make_client().post("/api/runs", json={}).status_code == 503
    assert read_control().paused


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/runs"),
        ("post", "/api/runs/example/start"),
        ("post", "/api/runs/example/resume"),
        ("post", "/api/runs/example/example-chat"),
        ("post", "/api/runs/example/messages"),
        ("post", "/api/runs/example/messages/ask"),
        ("post", "/api/runs/example/messages/1/revise"),
        ("post", "/api/runs/example/messages/started"),
        ("post", "/api/runs/example/attachments"),
        ("post", "/api/runs/example/attachments/upload"),
        ("post", "/api/interviews"),
        ("post", "/api/interviews/example/turns"),
        ("put", "/api/interviews/example/fields"),
        ("post", "/api/interviews/example/retry"),
    ],
)
def test_paused_ingress_refuses_all_funded_actions_before_body_processing(
    method: str, path: str
) -> None:
    _set_pause()
    response = getattr(make_client(), method)(path, json={})
    assert response.status_code == 503
    assert response.json()["code"] == "launch_paused"
    assert response.headers["cache-control"] == "no-store"


def test_pause_preserves_reads_cancellation_and_operator_resume(manual_worker: None) -> None:
    client = make_client()
    run_id = create_run(client, "synthetic goal").json()["id"]
    assert client.post(f"/api/runs/{run_id}/start", json={}).status_code == 200
    operator = make_operator_client()
    assert (
        operator.put(
            "/api/launch-control", json={"paused": True, "expected_revision": 0}
        ).status_code
        == 200
    )
    assert client.get(f"/api/runs/{run_id}").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/api/launch-status").json()["reason"] == "paused"
    assert runs.get_run(run_id).status == "queued"  # type: ignore[union-attr]
    assert client.post(f"/api/runs/{run_id}/cancel", json={}).status_code == 200
    assert (
        operator.put(
            "/api/launch-control", json={"paused": False, "expected_revision": 1}
        ).status_code
        == 200
    )
    assert create_run(client, "another synthetic goal").status_code == 200


def test_stale_updates_and_invalid_control_settings_are_rejected() -> None:
    operator = make_operator_client()
    assert (
        operator.put(
            "/api/launch-control", json={"paused": True, "expected_revision": 0}
        ).status_code
        == 200
    )
    assert (
        operator.put(
            "/api/launch-control", json={"paused": False, "expected_revision": 0}
        ).status_code
        == 409
    )
    for fields in (
        {"paused": False, "drain": True},
        {"paused": True, "message": " "},
        {"paused": True, "resumes_at": 1},
        {"paused": True, "message": "x" * 401},
    ):
        assert (
            operator.put("/api/launch-control", json={**fields, "expected_revision": 1}).status_code
            == 422
        )


def test_pause_after_setup_cannot_commit_a_new_run(monkeypatch: pytest.MonkeyPatch) -> None:
    original = crud._resolve_setup

    async def resolve(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        _set_pause()
        return result

    monkeypatch.setattr(crud, "_resolve_setup", resolve)
    assert create_run(make_client(), "synthetic race").status_code == 503
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_admissions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_pause_after_start_preparation_cannot_queue_work(
    monkeypatch: pytest.MonkeyPatch, manual_worker: None
) -> None:
    client = make_client()
    run_id = create_run(client, "synthetic start race").json()["id"]
    original = lifecycle._check_startable

    def check(run: RunRow) -> None:
        original(run)
        _set_pause()

    monkeypatch.setattr(lifecycle, "_check_startable", check)
    assert client.post(f"/api/runs/{run_id}/start", json={}).status_code == 503
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] == "draft"
        )
        assert conn.execute("SELECT COUNT(*) FROM scientific_tasks").fetchone()[0] == 0


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.COMPLETED])
@pytest.mark.parametrize("path", ["attachments", "attachments/upload", "messages"])
def test_pause_after_contribution_preparation_does_not_persist_input(
    monkeypatch: pytest.MonkeyPatch, status: RunStatus, path: str
) -> None:
    run = seed_run("synthetic input race", client_id=DEFAULT_TEST_CLIENT_ID)
    runs.update_run_status(run.id, status)
    seed_checkpoint(run.id, {}, stage="synthetic")
    module = chat if path == "messages" else contrib
    original = module._require_run

    def check(run_id: str) -> None:
        original(run_id)
        _set_pause()

    monkeypatch.setattr(module, "_require_run", check)
    client = make_client()
    if path == "attachments/upload":
        response = client.post(
            f"/api/runs/{run.id}/{path}",
            files={"file": ("synthetic.txt", b"synthetic evidence", "text/plain")},
            data={"consent": "true"},
        )
    else:
        body: dict[str, Any] = (
            {"content": "synthetic steering"}
            if path == "messages"
            else {"title": "synthetic", "text": "synthetic evidence", "consent": True}
        )
        response = client.post(f"/api/runs/{run.id}/{path}", json=body)
    assert response.status_code == 503
    assert response.json()["code"] == "launch_paused"
    with db.connect() as conn:
        for table in ("evidence", "messages", "run_events", "scientific_tasks"):
            assert (
                conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run.id,)).fetchone()[
                    0
                ]
                == 0
            )


@pytest.mark.parametrize("path", ["attachments", "attachments/upload", "messages"])
def test_pause_during_continuation_preparation_rolls_back_all_input(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    run = seed_run("synthetic continuation race", client_id=DEFAULT_TEST_CLIENT_ID)
    runs.update_run_status(run.id, RunStatus.COMPLETED)
    seed_checkpoint(run.id, {}, stage="synthetic")
    original = vars(inputs)["prepare_readmission"]

    def prepare(*args: Any, **kwargs: Any) -> Any:
        _set_pause()
        return original(*args, **kwargs)

    monkeypatch.setattr(inputs, "prepare_readmission", prepare)
    client = make_client()
    if path == "attachments/upload":
        response = client.post(
            f"/api/runs/{run.id}/{path}",
            files={"file": ("synthetic.txt", b"synthetic evidence", "text/plain")},
            data={"consent": "true"},
        )
    else:
        body: dict[str, Any] = (
            {"content": "synthetic steering"}
            if path == "messages"
            else {"title": "synthetic", "text": "synthetic evidence", "consent": True}
        )
        response = client.post(f"/api/runs/{run.id}/{path}", json=body)
    assert response.status_code == 503
    assert response.json()["code"] == "launch_paused"
    with db.connect() as conn:
        for table in ("evidence", "messages", "run_events", "scientific_tasks"):
            assert (
                conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run.id,)).fetchone()[
                    0
                ]
                == 0
            )


@pytest.mark.parametrize("path", ["ask", "started", "1/revise"])
def test_pause_after_chat_preparation_does_not_change_messages(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    from co_scientist.domains.chat.repository import messages

    run = seed_run("synthetic chat race", client_id=DEFAULT_TEST_CLIENT_ID)
    before = messages.append_message(messages.NewMessage(run.id, "user", "Original question", "qa"))
    original = vars(chat)["_run_or_404"]

    def check(run_id: str) -> RunRow:
        row = original(run_id)
        _set_pause()
        return cast(RunRow, row)

    monkeypatch.setattr(chat, "_run_or_404", check)
    body = (
        {"prompt": "synthetic prompt"} if path == "started" else {"question": "synthetic question"}
    )
    response = make_client().post(f"/api/runs/{run.id}/messages/{path}", json=body)
    assert response.status_code == 503
    assert response.json()["code"] == "launch_paused"
    assert messages.list_messages(run.id) == [before]


@pytest.mark.parametrize("action", ["create", "turn", "edit", "retry", "fields"])
def test_pause_after_interview_preparation_does_not_change_transcript(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from co_scientist.domains.chat.repository import interviews

    saved = interviews.create_interview(DEFAULT_TEST_CLIENT_ID, "Synthetic opening")
    interview_id = str(saved["id"])
    interviews.append_interview_turn(
        interview_id, interviews.NewInterviewTurn("agent", "Synthetic answer")
    )
    before = interviews.get_interview(interview_id)
    assert before is not None
    user_id, agent_id = (turn["id"] for turn in before["turns"])
    if action == "fields":
        original_owned = vars(interview_api)["_owned_interview"]

        def owned(*args: Any, **kwargs: Any) -> Any:
            result = original_owned(*args, **kwargs)
            _set_pause()
            return result

        monkeypatch.setattr(interview_api, "_owned_interview", owned)
    else:
        original_byok = vars(interview_api)["_request_byok"]

        def byok(*args: Any, **kwargs: Any) -> Any:
            result = original_byok(*args, **kwargs)
            _set_pause()
            return result

        monkeypatch.setattr(interview_api, "_request_byok", byok)
        monkeypatch.setattr(interview_turns, "request_byok", byok)
    client = make_client()
    if action == "create":
        response = client.post(
            "/api/interviews", json={"research_challenge": "Synthetic challenge"}
        )
    elif action == "turn":
        response = client.post(
            f"/api/interviews/{interview_id}/turns", json={"content": "Synthetic turn"}
        )
    elif action == "edit":
        response = client.put(
            f"/api/interviews/{interview_id}/turns/{user_id}", json={"content": "Replacement"}
        )
    elif action == "retry":
        response = client.post(f"/api/interviews/{interview_id}/turns/{agent_id}/retry")
    else:
        response = client.put(
            f"/api/interviews/{interview_id}/fields",
            json={"research_challenge": "Replacement", "focus_area": [], "preferences": []},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "launch_paused"
    assert interviews.get_interview(interview_id) == before
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 1


def test_example_copy_checks_pause_inside_its_transaction() -> None:
    from co_scientist.domains.chat.repository.examples import open_example_chat

    _set_pause()
    with pytest.raises(LaunchPausedError):
        open_example_chat("synthetic example", DEFAULT_TEST_CLIENT_ID)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 0


def test_continuation_checks_pause_inside_its_transaction() -> None:
    run = seed_run("synthetic continuation", client_id=DEFAULT_TEST_CLIENT_ID)
    runs.update_run_status(run.id, RunStatus.COMPLETED)
    seed_checkpoint(run.id, {}, stage="synthetic")
    _set_pause()
    with pytest.raises(LaunchPausedError):
        engine_tasks.enqueue_scientist_continuation(run.id, 42)
    assert runs.get_run(run.id).status == "completed"  # type: ignore[union-attr]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scientific_tasks").fetchone()[0] == 0


def test_status_polling_does_not_write_or_wait_for_the_writer(isolated_db: str) -> None:
    client = make_client()
    _set_pause()
    with db.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        before = writer.total_changes
        started = time.monotonic()
        for _ in range(10):
            assert client.get("/api/launch-status").status_code == 200
        assert time.monotonic() - started < 2
        assert writer.total_changes == before
        writer.execute("ROLLBACK")
    with db.connect(isolated_db) as conn:
        assert conn.execute("SELECT revision FROM launch_control").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_default_pause_allows_an_admitted_reply_to_finish_in_one_credential_context() -> None:
    credential: contextvars.ContextVar[str] = contextvars.ContextVar(
        "synthetic-credential", default=""
    )
    closed = False

    async def source() -> AsyncIterator[str]:
        nonlocal closed
        token = credential.set("private")
        try:
            yield "first"
            await asyncio.sleep(0)
            assert credential.get() == "private"
            yield "second"
        finally:
            credential.reset(token)
            closed = True

    response = launch_admission.chat_response(StreamingResponse(source()))
    iterator = aiter(response.body_iterator)
    assert await anext(iterator) == "first"
    assert credential.get() == ""
    _set_pause()
    assert await anext(iterator) == "second"
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert closed and credential.get() == ""


@pytest.mark.asyncio
async def test_drain_closes_a_silent_reply_even_after_a_rapid_resume() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    async def source() -> AsyncIterator[str]:
        try:
            started.set()
            await asyncio.Event().wait()
            yield "never"
        finally:
            closed.set()

    response = launch_admission.chat_response(StreamingResponse(source()))
    reader = asyncio.ensure_future(anext(aiter(response.body_iterator)))
    await asyncio.wait_for(started.wait(), 1)
    _set_pause(drain=True)
    _set_pause(paused=False)
    fragment = await asyncio.wait_for(reader, 2)
    assert "error" in str(fragment)
    assert closed.is_set()
    await response.body_iterator.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_operator_drain_cancels_worker_before_lease_renewal(
    monkeypatch: pytest.MonkeyPatch,
    isolated_db: str,
) -> None:
    run = seed_run("synthetic leased run", client_id=DEFAULT_TEST_CLIENT_ID)
    runs.update_run_status(run.id, RunStatus.RUNNING)
    task = enqueue_task(run.id, "engine.test.silent", "synthetic", db_path=isolated_db)
    started = asyncio.Event()
    closed = asyncio.Event()

    async def execute(task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        try:
            started.set()
            await asyncio.Event().wait()
            return {}
        finally:
            closed.set()

    monkeypatch.setattr(engine_tasks, "execute_engine_task", execute)
    worker = asyncio.create_task(task_worker.run_once("synthetic-worker", db_path=isolated_db))
    await asyncio.wait_for(started.wait(), 1)
    response = make_operator_client().put(
        "/api/launch-control",
        json={
            "paused": True,
            "drain": True,
            "expected_revision": 0,
        },
    )
    assert response.status_code == 200 and response.json()["cancelled_runs"] == 1
    await asyncio.wait_for(worker, 2)
    assert closed.is_set()
    assert tasks.get_task(task.id, db_path=isolated_db).status == "cancelled"  # type: ignore[union-attr]
    assert runs.get_run(run.id).status == "cancelled"  # type: ignore[union-attr]


def test_operator_control_survives_exhausted_visitor_write_quotas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "anonymous_write_global_requests_per_day", 1)
    client = make_client()
    assert create_run(client, "synthetic quota").status_code == 200
    assert create_run(client, "synthetic refusal").status_code == 429
    _set_pause()
    operator = make_operator_client()
    response = operator.put("/api/launch-control", json={"paused": False, "expected_revision": 1})
    assert response.status_code == 200
    assert operator.put("/api/launch-control", content="x" * 4097).status_code == 413


def test_late_heartbeat_can_renew_owned_lease_but_not_reclaimed_or_cancelled_lease(
    isolated_db: str,
) -> None:
    run = seed_run("synthetic lease renewal", db_path=isolated_db)
    task = enqueue_task(run.id, "engine.test.silent", "synthetic", db_path=isolated_db)
    assert tasks.claim_task("first", db_path=isolated_db) is not None
    with db.transaction(isolated_db) as conn:
        conn.execute("UPDATE scientific_tasks SET lease_expires_at=0 WHERE id=?", (task.id,))
    assert owns_task_lease(task.id, "first", db_path=isolated_db)
    assert tasks.renew_task_lease(task.id, "first", 60, db_path=isolated_db)
    with db.transaction(isolated_db) as conn:
        conn.execute("UPDATE scientific_tasks SET lease_expires_at=0 WHERE id=?", (task.id,))
    assert tasks.claim_task("second", db_path=isolated_db) is not None
    assert not owns_task_lease(task.id, "first", db_path=isolated_db)
    assert not tasks.renew_task_lease(task.id, "first", 60, db_path=isolated_db)
    assert owns_task_lease(task.id, "second", db_path=isolated_db)
    tasks.cancel_run_tasks(run.id, db_path=isolated_db)
    assert not owns_task_lease(task.id, "second", db_path=isolated_db)
    assert not tasks.renew_task_lease(task.id, "second", 60, db_path=isolated_db)


def test_drain_preserves_unknown_paid_reservations_and_does_not_replay_them() -> None:
    run = seed_run("synthetic funded run", client_id=DEFAULT_TEST_CLIENT_ID)
    runs.update_run_status(run.id, RunStatus.RUNNING)
    with db.transaction(durable=True) as conn:
        conn.execute(
            "INSERT INTO llm_spend "
            "(id,created_at,model,role,reserved_microeur,charged_microeur,"
            "input_bound,output_bound,rates) "
            "VALUES ('synthetic-paid',0,'synthetic','synthetic',1000000,1000000,1,1,'{}')"
        )
        conn.execute(
            "INSERT INTO provider_token_reservations "
            "VALUES ('synthetic-paid',0,?, 'peer',10,0,NULL)",
            (DEFAULT_TEST_CLIENT_ID,),
        )
    assert (
        make_operator_client()
        .put("/api/launch-control", json={"paused": True, "drain": True, "expected_revision": 0})
        .status_code
        == 200
    )
    with db.connect() as conn:
        assert tuple(
            conn.execute(
                "SELECT charged_microeur,settled FROM llm_spend WHERE id='synthetic-paid'"
            ).fetchone()
        ) == (1000000, 0)
        assert (
            conn.execute(
                "SELECT used_tokens FROM provider_token_reservations WHERE id='synthetic-paid'"
            ).fetchone()[0]
            is None
        )


def test_capacity_and_credit_notices_use_real_ledgers_and_never_invent_an_azure_reset(
    fake_process_mode: FakeProcessMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process_mode.online()
    client = make_client()
    monkeypatch.setattr(settings, "free_runs_globally_per_day", 1)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO run_admissions VALUES (?,?,?,?,1)",
            ("synthetic", "other", "peer", int(db.current_time() // 86400)),
        )
    notice = client.get("/api/launch-status").json()
    assert notice["reason"] == "free_capacity"
    assert notice["resumes_at"] == (int(db.current_time() // 86400) + 1) * 86400
    assert not notice["free_runs_allowed"]
    assert notice["byok_runs_allowed"]
    monkeypatch.setattr(settings, "free_runs_globally_per_day", 20)
    monkeypatch.setenv("LLM_AZURE_ENABLED", "1")
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "1")
    monkeypatch.setenv("LLM_AZURE_EXPIRES_AT", "2100-01-01T00:00:00+00:00")
    monkeypatch.setenv("LLM_USD_TO_EUR", "0.88")
    record_azure_allowance()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO llm_spend (id,created_at,model,role,reserved_microeur,charged_microeur,"
            "input_bound,output_bound,rates) "
            "VALUES ('synthetic',0,'synthetic','synthetic',1000000,1000000,1,1,'{}')"
        )
    launch_control_api._cached_credit.cache_clear()
    notice = client.get("/api/launch-status").json()
    assert notice["reason"] == "credit_exhausted" and notice["resumes_at"] is None
    assert "charged_and_reserved_microeur" not in notice
    assert (
        make_operator_client()
        .get("/api/launch-control")
        .json()["credit"]["charged_and_reserved_microeur"]
        == 1000000
    )
