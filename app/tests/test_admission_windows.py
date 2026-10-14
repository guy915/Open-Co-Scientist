from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from co_scientist.api import logs_api
from co_scientist.core.admission_windows import WindowExceededError, utc_day, utc_day_bounds
from co_scientist.domains.access.admission import reserve_memory_window
from co_scientist.domains.feedback import repository as feedback
from co_scientist.platform import db
from fastapi import HTTPException
from starlette.requests import Request


def test_refused_second_log_scope_does_not_charge_first_and_returns_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(logs_api, "client_id", lambda request: "owner")
    request = Request({"type": "http", "client": ("127.0.0.1", 1), "headers": []})
    hits = {"id:owner": [90.0]}
    with pytest.raises(HTTPException) as denied:
        logs_api._check_both_rates(hits, request, 1, "too many logs")
    assert hits == {"id:owner": [90.0]}
    assert denied.value.status_code == 429
    assert denied.value.headers == {"Retry-After": "50"}


def test_threaded_memory_window_admits_exact_capacity_across_scopes() -> None:
    hits: dict[str, list[float]] = {}
    lock = threading.Lock()

    def attempt(index: int) -> bool:
        try:
            reserve_memory_window(
                hits, ["host", f"owner:{index}"], lock=lock, limit=5, now=100.0, detail="limit"
            )
        except WindowExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = list(pool.map(attempt, range(100)))
    assert sum(accepted) == 5
    assert len(hits["host"]) == 5
    assert sum(len(v) for k, v in hits.items() if k != "host") == 5
    reserve_memory_window(hits, ["host", "next"], lock=lock, limit=5, now=160.0, detail="limit")
    assert hits == {"host": [160.0], "next": [160.0]}


def test_feedback_window_survives_reconnection_and_expires_at_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(db, "current_time", lambda: now[0])
    submission = feedback.Submission(category="Bug", message="report", diagnostics="", url="/")
    for _ in range(5):
        feedback.submit("owner", "host", submission)
    now[0] = 40.0
    with pytest.raises(feedback.RateExceededError) as denied:
        feedback.submit("owner", "host", submission)
    assert denied.value.retry_after == 20
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback_admissions").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 5
    now[0] = 60.0
    feedback.submit("owner", "host", submission)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback_admissions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 6


@pytest.mark.parametrize(
    "now,day,bounds",
    [
        (-0.1, -1, (-86400.0, 0.0)),
        (0.0, 0, (0.0, 86400.0)),
        (86399.9, 0, (0.0, 86400.0)),
        (86400.0, 1, (86400.0, 172800.0)),
    ],
)
def test_daily_admission_shares_exact_utc_midnight(
    now: float, day: int, bounds: tuple[float, float]
) -> None:
    assert utc_day(now) == day
    assert utc_day_bounds(now) == bounds


def test_disabled_memory_limit_does_not_record_hits() -> None:
    hits: dict[str, list[float]] = {}
    reserve_memory_window(hits, ["owner"], lock=threading.Lock(), limit=0, now=0.0, detail="limit")
    assert hits == {}
