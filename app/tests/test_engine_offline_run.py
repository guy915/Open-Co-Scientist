from __future__ import annotations

from typing import Any

import pytest
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.orchestration.repository import events as store_events
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus
from co_scientist.platform.llm.offline import llm as offline_llm
from co_scientist.platform.llm.request import backend

from tests._store_helpers import drive_offline_run, seed_run

from ._llm_fake_backend import load_engine_fake


@pytest.fixture(autouse=True)
def _isolate_offline_router(monkeypatch: pytest.MonkeyPatch) -> None:
    # Offline backend registration is process-global; restore it and clear
    # idempotency state.
    monkeypatch.setattr(backend, "_installed", backend._installed)
    monkeypatch.setattr(offline_llm, "_installed", False)


def _install_recording_router(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    # Record escaped provider calls while answering offline so leaks cannot make
    # paid calls.
    escaped_calls: list[dict[str, Any]] = []

    async def _recording_backend(**kwargs: Any) -> Any:
        escaped_calls.append(kwargs)
        return await offline_llm.offline_acompletion(**kwargs)

    load_engine_fake().install_fake_backend(monkeypatch, _recording_backend)
    offline_llm.install_offline_router()
    return escaped_calls


def test_offline_engine_run_completes_without_a_real_call(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.core.config import settings

    monkeypatch.setattr(settings, "semantic_safety_enabled", False)
    escaped_calls = _install_recording_router(monkeypatch)
    run = seed_run(
        "Explain how protein X folds under crowding.",
        profile="express",
        provider="mock",
        config={
            "tier": "express",
            "enable_literature_review": False,
            "llm_backend": "offline",
        },
        client_id="offline-e2e",
        llm_backend="offline",
        db_path=isolated_db,
    )

    drive_offline_run(run, db_path=isolated_db, worker="offline-e2e-test")

    assert not escaped_calls, (
        "a call reached the wrapped backend instead of the offline "
        f"router: {escaped_calls[0].get('model')!r}"
    )
    events = store_events.list_events(run.id, db_path=isolated_db)
    assert "report" in [e["type"] for e in events]
    final = runs.get_run(run.id, db_path=isolated_db)
    assert final is not None
    assert final.status == RunStatus.COMPLETED.value
    assert final.llm_backend == "offline"
    assert reports.get_latest_report(run.id, db_path=isolated_db) is not None
    assert hypotheses.list_hypotheses(run.id, db_path=isolated_db)
