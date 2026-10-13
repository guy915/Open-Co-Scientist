from __future__ import annotations

import json
import socket
from threading import Event, Thread, Timer

import httpx
import pytest
import uvicorn
from co_scientist.api.runs import events as run_events
from co_scientist.orchestration.repository.events import append_event
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus

from tests._client import DEFAULT_TEST_CLIENT_ID, make_client, make_operator_client, wait_for
from tests._store_helpers import seed_run


def test_an_idle_live_stream_sends_keepalive_comments_until_it_ends(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_events, "_TICK_SECONDS", 0.01)
    monkeypatch.setattr(run_events, "_KEEPALIVE_TICKS", 5)
    run = seed_run("Quiet run", client_id=DEFAULT_TEST_CLIENT_ID, db_path=isolated_db)
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)

    timer = Timer(
        0.4, lambda: runs.update_run_status(run.id, RunStatus.COMPLETED, db_path=isolated_db)
    )
    timer.start()
    body = make_client().get(f"/api/runs/{run.id}/events").text
    timer.join()

    frames = [frame for frame in body.split("\n\n") if frame]
    assert frames.count(": keepalive") >= 2
    data = [json.loads(frame[6:]) for frame in frames if frame.startswith("data: ")]
    assert [frame["type"] for frame in data] == ["_terminal"]
    assert data[0]["payload"]["status"] == "completed"


def test_quiesce_closes_live_stream_without_terminating_the_run(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_events, "_TICK_SECONDS", 0.01)
    run = seed_run("Deploying run", client_id=DEFAULT_TEST_CLIENT_ID, db_path=isolated_db)
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    quiesce = Timer(0.05, lambda: make_operator_client().post("/api/deployment/quiesce"))
    fallback = Timer(
        0.8, lambda: runs.update_run_status(run.id, RunStatus.COMPLETED, db_path=isolated_db)
    )
    quiesce.start()
    fallback.start()
    try:
        body = make_client().get(f"/api/runs/{run.id}/events").text
        assert '"_terminal"' not in body
        current = runs.get_run(run.id, db_path=isolated_db)
        assert current is not None and current.status == RunStatus.RUNNING
        assert make_client().get(f"/api/runs/{run.id}/events").text == ""
    finally:
        fallback.cancel()
        quiesce.join()
        fallback.join()


def test_quiesced_reader_allows_uvicorn_to_shutdown_without_cancelling_requests(
    isolated_db: str,
) -> None:
    from co_scientist.core.config import settings
    from co_scientist.main import app

    run = seed_run("Deploying run", client_id=DEFAULT_TEST_CLIENT_ID, db_path=isolated_db)
    runs.update_run_status(run.id, RunStatus.RUNNING, db_path=isolated_db)
    append_event(run.id, "progress", {"step": 1}, db_path=isolated_db)
    opened = Event()
    failures: list[BaseException] = []
    frames: list[str] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(
            uvicorn.Config(app, lifespan="off", log_config=None, timeout_graceful_shutdown=1)
        )
        serving = Thread(target=server.run, kwargs={"sockets": [listener]})
        serving.start()

        def read_stream() -> None:
            try:
                with httpx.stream(
                    "GET",
                    f"{url}/api/runs/{run.id}/events",
                    headers={"X-Client-ID": DEFAULT_TEST_CLIENT_ID},
                    timeout=5,
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        frames.append(line)
                        if line.startswith("data:"):
                            opened.set()
            except BaseException as exc:
                failures.append(exc)

        reader = Thread(target=read_stream)
        try:
            assert wait_for(lambda: server.started, timeout=5)
            reader.start()
            assert opened.wait(5)
            headers = {"X-Logs-Token": settings.logs_admin_token or ""}
            assert httpx.post(f"{url}/api/deployment/quiesce", headers=headers).status_code == 200
            reader.join(5)
            assert not reader.is_alive() and not failures
            assert httpx.get(f"{url}/api/deployment/quiesce", headers=headers).json()["ready"]
            assert not any('"_terminal"' in frame for frame in frames)
        finally:
            server.should_exit = True
            serving.join(5)
            if reader.ident is not None:
                reader.join(5)
        assert not serving.is_alive()
        current = runs.get_run(run.id, db_path=isolated_db)
        assert current is not None and current.status == RunStatus.RUNNING
