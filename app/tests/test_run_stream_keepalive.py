from __future__ import annotations

import json
from threading import Timer

import pytest
from co_scientist.api.runs import events as run_events
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus

from tests._client import DEFAULT_TEST_CLIENT_ID, make_client
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
