from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from co_scientist.api import logs_api
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.domains.chat.repository import messages
from co_scientist.platform import db
from co_scientist.platform.llm import llm_request, offline_guard, provider_usage

from tests._client import create_run, fake_litellm, make_client
from tests._llm_fake_backend import install_completion_backend


@pytest.fixture(autouse=True)
def _fresh_log_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rate scopes outlive isolated test databases and xdist changes test order.
    monkeypatch.setattr(logs_api, "_ingest_hits", {})


def test_log_association_requires_owner_and_rolls_back_whole_batch() -> None:
    client = make_client()
    run_id = create_run(client, "Test ownership", tier="express").json()["id"]
    records = [{"message": "first"}, {"message": "forged", "run_id": run_id}]
    result = client.post("/api/logs", headers={"X-Client-ID": "other"}, json={"records": records})
    assert result.status_code == 404
    visible = client.get("/api/logs", headers={"X-Client-ID": "other"}).json()["logs"]
    assert not any(row["message"] in {"first", "forged"} for row in visible)
    assert client.post("/api/logs", json={"records": records}).status_code == 200


@pytest.mark.parametrize("owner,run_id", [("", "missing"), ("owner", "missing"), ("owner", "")])
def test_logs_reject_unknown_or_anonymous_run(owner: str, run_id: str) -> None:
    result = make_client().post(
        "/api/logs",
        headers={"X-Client-ID": owner},
        json={"records": [{"message": "x", "run_id": run_id}]},
    )
    assert result.status_code == 404


def test_provider_budget_reservation_is_atomic_and_durable(
    monkeypatch: pytest.MonkeyPatch,
    isolated_db: str,
) -> None:
    monkeypatch.setattr(settings, "app_llm_client_calls_per_day", 3)

    def attempt(_: int) -> bool:
        with provider_usage.scoped_client("one"):
            try:
                provider_usage.reserve({"max_tokens": 10, "messages": []})
                return True
            except ProviderAdmissionError:
                return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(12))) == 3
    db._initialized.discard(isolated_db)
    assert not attempt(0)


def test_rotating_client_ids_cannot_reset_global_provider_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_llm_global_calls_per_day", 2)
    for owner in ("one", "two"):
        with provider_usage.scoped_client(owner):
            provider_usage.reserve({"max_tokens": 10})
    with provider_usage.scoped_client("three"), pytest.raises(ProviderAdmissionError):
        provider_usage.reserve({"max_tokens": 10})


def test_token_reservations_include_input_and_do_not_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_llm_client_tokens_per_day", 1200)
    with provider_usage.scoped_client("one"):
        provider_usage.reserve({"max_tokens": 100})
        with pytest.raises(ProviderAdmissionError):
            provider_usage.reserve({"max_tokens": 100})
    with provider_usage.scoped_client("two"), pytest.raises(ProviderAdmissionError):
        provider_usage.reserve({"max_tokens": 100, "messages": [{"content": "x" * 2000}]})


async def test_budget_stops_dispatch_and_byok_keeps_its_own_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider(**kwargs: Any) -> Any:
        return SimpleNamespace(choices=[], model="gpt-4o-mini")

    monkeypatch.setattr(offline_guard, "require_remote_chat", lambda _: None)
    monkeypatch.setattr(settings, "app_llm_client_calls_per_day", 1)
    fake = install_completion_backend(monkeypatch, provider)
    with provider_usage.scoped_client("one"):
        await llm_request.acompletion(model="gpt-4o-mini", max_tokens=100)
        with pytest.raises(ProviderAdmissionError):
            await llm_request.acompletion(model="gpt-4o-mini", max_tokens=100)
        monkeypatch.setattr(byok_scope, "current_byok", lambda: object())
        await llm_request.acompletion(model="gpt-4o-mini", max_tokens=100)
    assert len(fake.requests) == 2


def test_announcement_replays_without_duplicate_prompt_or_provider_call() -> None:
    client = make_client()
    run_id = create_run(client, "Research test", tier="express").json()["id"]
    url = f"/api/runs/{run_id}/messages/started"
    first = client.post(url, json={"prompt": "Start"})
    second = client.post(url, json={"prompt": "Repeat"})
    assert first.status_code == second.status_code == 200
    assert first.text == second.text
    assert len([m for m in messages.list_messages(run_id) if m.kind == "start"]) == 2


def test_announcement_claims_are_atomic_even_before_streaming() -> None:
    run_id = create_run(make_client(), "Research test", tier="express").json()["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: messages.claim_start_prompt(run_id, "Start"), range(12)))
    assert sum(fresh for _, fresh in results) == 1
    assert len({message.id for message, _ in results}) == 1
    from co_scientist.domains.chat.run_start_announcement import replay_announcement

    async def replay() -> str:
        return "".join([frame async for frame in replay_announcement(run_id, results[0][0].id)])

    assert '"fallback": true' in asyncio.run(replay())


def test_streamed_http_calls_keep_owner_budget_across_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    first = create_run(client, "First", tier="express").json()["id"]
    second = create_run(client, "Second", tier="express").json()["id"]
    other = create_run(client, "Other", tier="express", headers={"X-Client-ID": "other"}).json()[
        "id"
    ]
    monkeypatch.setattr(offline_guard, "require_remote_chat", lambda _: None)
    monkeypatch.setattr(settings, "app_llm_client_calls_per_day", 1)
    calls: list[dict[str, Any]] = []
    install_completion_backend(monkeypatch, fake_litellm(["Started."], calls=calls).acompletion)

    def announce(run_id: str, owner: str) -> str:
        return client.post(
            f"/api/runs/{run_id}/messages/started",
            headers={"X-Client-ID": owner},
            json={"prompt": "Start"},
        ).text

    assert '"fallback": false' in announce(first, "pytest-default-client")
    assert '"fallback": true' in announce(second, "pytest-default-client")
    assert '"fallback": false' in announce(other, "other")
    announce(first, "pytest-default-client")
    assert len(calls) == 2
    with db.connect() as conn:
        rows = conn.execute("SELECT client_id,calls FROM app_llm_usage").fetchall()
    assert {row["client_id"]: row["calls"] for row in rows} == {
        "pytest-default-client": 1,
        "other": 1,
    }


def test_concurrent_log_ingestion_does_not_crash_the_rate_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """post_logs runs on a four-thread handler pool, and the window expired
    stale scopes by deleting from the dict it was iterating.
    """
    monkeypatch.setattr(settings, "logs_ingest_per_minute", 10_000)

    def hammer(worker: int) -> None:
        for index in range(400):
            request = SimpleNamespace(
                client=SimpleNamespace(host=f"10.0.{worker}.{index % 255}"),
                headers={"X-Client-ID": f"client-{worker}-{index}"},
                cookies={},
            )
            logs_api._check_ingest_rate(request)  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=8) as pool:
        for outcome in [pool.submit(hammer, worker) for worker in range(8)]:
            outcome.result()
