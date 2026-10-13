from __future__ import annotations

import logging

import pytest
from co_scientist.orchestration.repository import events as store

from tests._store_helpers import seed_run


def _stage_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.run_stage"]


@pytest.fixture
def run_id(isolated_db: str) -> str:
    run = seed_run("stage logging", provider="mock", db_path=isolated_db)
    return str(run.id)


@pytest.mark.parametrize(
    ("event", "payload", "message"),
    [
        ("generate", {"count": 4}, "generate count=4 activity=drafting"),
        (
            "safety.intake",
            {"stage": "intake", "decision": "allow", "reason": ""},
            "safety.intake stage=intake decision=allow activity=safety",
        ),
        (
            "ranking",
            {"iteration": 2, "matches": [1, 2, 3]},
            "ranking iteration=2 matches=3 activity=tournament",
        ),
    ],
)
def test_append_event_logs_a_compact_stage_record(
    isolated_db: str,
    run_id: str,
    caplog: pytest.LogCaptureFixture,
    event: str,
    payload: dict[str, object],
    message: str,
) -> None:
    with caplog.at_level(logging.INFO):
        store.append_event(run_id, event, payload, db_path=isolated_db)
    [record] = _stage_records(caplog)
    assert record.getMessage() == message
    assert getattr(record, "run_id", None) == run_id


def test_stage_logging_never_breaks_the_event_write(
    isolated_db: str, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("logging is down")

    monkeypatch.setattr("co_scientist.orchestration.repository.events._log_stage", boom)
    seq = store.append_event(run_id, "generate", {"count": 1}, db_path=isolated_db)
    assert seq >= 1
    assert len(store.list_events(run_id, db_path=isolated_db)) == 1
