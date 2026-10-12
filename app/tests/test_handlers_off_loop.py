from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from co_scientist.domains.research_state.repository import hypotheses

from tests._client import DEFAULT_TEST_CLIENT_ID, make_client
from tests._store_helpers import seed_run


def test_a_handler_blocked_in_the_store_does_not_stall_other_requests(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("Blocked read", client_id=DEFAULT_TEST_CLIENT_ID, db_path=isolated_db)
    entered, release = threading.Event(), threading.Event()
    real = hypotheses.list_hypotheses

    def blocked(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        entered.set()
        release.wait(10)
        rows: list[dict[str, Any]] = real(*args, **kwargs)
        return rows

    monkeypatch.setattr(hypotheses, "list_hypotheses", blocked)
    with make_client() as client:
        slow = threading.Thread(target=client.get, args=(f"/api/runs/{run.id}/hypotheses",))
        slow.start()
        assert entered.wait(5)
        started = time.monotonic()
        assert client.get("/api/runs").status_code == 200
        elapsed = time.monotonic() - started
        release.set()
        slow.join()

    assert elapsed < 2
