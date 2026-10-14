from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from co_scientist.core.config import settings
from co_scientist.core.exceptions import LLMCallBudgetExceededError, ProviderAdmissionError
from co_scientist.platform import db
from co_scientist.platform.llm import provider_usage
from co_scientist.platform.llm.request.transport import complete_request

from tests._client import create_run, make_client
from tests._llm_fake_backend import install_completion_backend
from tests._process_mode_helpers import FakeProcessMode


async def test_scientific_calls_share_durable_global_admission(
    monkeypatch: pytest.MonkeyPatch, isolated_db: str
) -> None:
    monkeypatch.setattr(settings, "app_llm_global_calls_per_day", 2)

    async def respond(**kwargs: Any) -> Any:
        return SimpleNamespace(choices=[], model="gpt-4o-mini")

    fake = install_completion_backend(monkeypatch, respond)
    for owner in ("first", "second"):
        with provider_usage.scoped_client(owner):
            await complete_request(
                {"model": "gpt-4o-mini", "max_tokens": 10},
                "gpt-4o-mini",
                byok=False,
                timeout_seconds=1,
            )
    db._initialized.discard(isolated_db)
    with provider_usage.scoped_client("third"), pytest.raises(LLMCallBudgetExceededError):
        await complete_request(
            {"model": "gpt-4o-mini", "max_tokens": 10},
            "gpt-4o-mini",
            byok=False,
            timeout_seconds=1,
        )
    assert len(fake.requests) == 2


def test_active_run_limit_spans_anonymous_owners(
    monkeypatch: pytest.MonkeyPatch, manual_worker: None
) -> None:
    monkeypatch.setattr(settings, "max_concurrent_runs", 1)
    client = make_client()
    first = create_run(client, "First", tier="express").json()["id"]
    second = create_run(client, "Second", tier="express", headers={"X-Client-ID": "second"}).json()[
        "id"
    ]
    assert client.post(f"/api/runs/{first}/start", json={}).status_code == 200
    denied = client.post(f"/api/runs/{second}/start", json={}, headers={"X-Client-ID": "second"})
    assert denied.status_code == 409
    assert (
        client.get(f"/api/runs/{second}", headers={"X-Client-ID": "second"}).json()["status"]
        == "draft"
    )


def _set_new_limit(monkeypatch: pytest.MonkeyPatch, name: str, value: int) -> None:
    # Before the fix these knobs do not exist; the reproduction must fail on
    # admitted requests rather than on missing configuration attributes.
    if name in type(settings).model_fields:
        monkeypatch.setattr(settings, name, value)
    else:
        monkeypatch.setattr(type(settings), name, value, raising=False)


def test_rotating_sessions_and_forwarded_headers_cannot_reset_host_admission(
    monkeypatch: pytest.MonkeyPatch, isolated_db: str
) -> None:
    _set_new_limit(monkeypatch, "anonymous_sessions_per_host_per_day", 2)
    client = make_client()
    for owner in ("first", "second"):
        assert (
            create_run(client, "First", tier="express", headers={"X-Client-ID": owner}).status_code
            == 200
        )
    db._initialized.discard(isolated_db)
    denied = create_run(
        client,
        "Third",
        tier="express",
        headers={"X-Client-ID": "third", "X-Forwarded-For": "203.0.113.123"},
    )
    assert denied.status_code == 429
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


async def test_failed_scientific_dispatch_keeps_global_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_llm_global_calls_per_day", 1)

    async def fail(**kwargs: Any) -> Any:
        raise RuntimeError("provider outcome unknown")

    fake = install_completion_backend(monkeypatch, fail)
    with provider_usage.scoped_client("first"), pytest.raises(RuntimeError):
        await complete_request(
            {"model": "gpt-4o-mini", "max_tokens": 10},
            "gpt-4o-mini",
            byok=False,
            timeout_seconds=1,
        )
    with provider_usage.scoped_client("second"), pytest.raises(LLMCallBudgetExceededError):
        await complete_request(
            {"model": "gpt-4o-mini", "max_tokens": 10},
            "gpt-4o-mini",
            byok=False,
            timeout_seconds=1,
        )
    assert len(fake.requests) == 1


def test_rejected_app_quota_does_not_consume_global_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_llm_client_calls_per_day", 1)
    monkeypatch.setattr(settings, "app_llm_global_calls_per_day", 2)
    with provider_usage.scoped_client("first"):
        provider_usage.reserve({"max_tokens": 10})
        with pytest.raises(ProviderAdmissionError):
            provider_usage.reserve({"max_tokens": 10})
    with provider_usage.scoped_client("second"):
        provider_usage.reserve_physical({"max_tokens": 10})
    with db.connect() as conn:
        assert (
            conn.execute("SELECT calls FROM provider_admissions WHERE scope='global'").fetchone()[0]
            == 2
        )


def test_free_host_allowance_survives_owner_rotation_and_run_deletion(
    monkeypatch: pytest.MonkeyPatch,
    fake_process_mode: FakeProcessMode,
) -> None:
    fake_process_mode.online()
    monkeypatch.setattr(settings, "free_runs_per_host_per_day", 1)

    async def no_title(goal: str) -> None:
        return None

    monkeypatch.setattr("co_scientist.api.runs.crud.generate_run_title", no_title)
    client = make_client()
    run_id = create_run(client, "First", tier="express").json()["id"]
    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    assert (
        create_run(client, "Second", tier="express", headers={"X-Client-ID": "second"}).status_code
        == 429
    )
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM free_run_usage").fetchone()[0] == 1


def test_physical_token_admission_is_global_and_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_llm_global_tokens_per_day", 1300)
    with provider_usage.scoped_client("first"):
        provider_usage.reserve_physical({"max_tokens": 100})
    with provider_usage.scoped_client("second"), pytest.raises(LLMCallBudgetExceededError):
        provider_usage.reserve_physical({"max_tokens": 100})


def test_global_physical_admission_serializes_different_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_llm_global_calls_per_day", 2)

    def reserve(index: int) -> bool:
        with provider_usage.scoped_client(str(index)):
            try:
                provider_usage.reserve_physical({"max_tokens": 10})
                return True
            except LLMCallBudgetExceededError:
                return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(12))) == 2


def test_host_physical_admission_spans_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "provider_host_calls_per_day", 1)
    with provider_usage.scoped_client("first", host="connecting-peer"):
        provider_usage.reserve_physical({"max_tokens": 10})
    with (
        provider_usage.scoped_client("second", host="connecting-peer"),
        pytest.raises(LLMCallBudgetExceededError),
    ):
        provider_usage.reserve_physical({"max_tokens": 10})
    with provider_usage.scoped_client("third", host="different-peer"):
        provider_usage.reserve_physical({"max_tokens": 10})
