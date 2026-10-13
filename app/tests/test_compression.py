from __future__ import annotations

from co_scientist.platform.db import runs as store
from co_scientist.platform.db.models import RunStatus

from tests._client import make_client
from tests._store_helpers import seed_run


def test_large_json_responses_are_gzipped_and_event_streams_are_not(
    isolated_db: str,
) -> None:
    client = make_client()
    run = seed_run("Explain " * 400, client_id="gzip-owner", db_path=isolated_db)
    store.update_run_status(run.id, RunStatus.COMPLETED, db_path=isolated_db)
    headers = {"X-Client-ID": "gzip-owner", "Accept-Encoding": "gzip"}

    detail = client.get(f"/api/runs/{run.id}", headers=headers)
    assert detail.status_code == 200
    assert detail.headers["content-encoding"] == "gzip"
    assert detail.json()["id"] == run.id

    with client.stream("GET", f"/api/runs/{run.id}/events", headers=headers) as events:
        assert events.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in events.headers
