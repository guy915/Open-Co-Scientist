from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import connect, runs, transaction
from co_scientist.platform.db.llm_routes import (
    OpenRouterCapacityError,
    admit_route,
    free_available,
    reserve_free_call,
)
from co_scientist.platform.db.models import RunStatus
from co_scientist.platform.db.runs import RunCreateOptions
from co_scientist.platform.db.spend import SpendReservation, reserve_spend, settle_spend

from tests._azure_ledger import record_azure_allowance


@pytest.fixture
def path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = str(tmp_path / "routes.db")
    monkeypatch.setenv("COSCIENTIST_DB_PATH", path)
    record_azure_allowance(path)
    return path


def _run(path: str, tier: str = "express") -> str:
    return runs.create_run(
        "synthetic goal",
        tier,
        "engine",
        {"tier": tier},
        RunCreateOptions(client_id="owner", llm_backend="real", db_path=path),
    ).id


_RATES = '{"input": "9", "output": "9", "cached": "9", "write": "9", "fx": "9"}'


def _quote(run_id: str, amount: int = 60) -> SpendReservation:
    return SpendReservation(
        "azure/gpt-6-luna-2026-09-22", "worker", amount, 100, float("inf"), 100, 100, _RATES, run_id
    )


def test_run_forecast_must_fit_and_standard_is_never_operator_funded(path: str) -> None:
    first, second, standard = _run(path), _run(path), _run(path, "standard")
    with transaction(path) as conn:
        one = admit_route(conn, first, slots=("anthropic", "azure"), estimate=70, total=100)
        two = admit_route(conn, second, slots=("anthropic", "azure"), estimate=70, total=100)
        assert one.azure_allowed and not two.azure_allowed
        assert two.slot == "anthropic"
        with pytest.raises(ProviderAdmissionError):
            admit_route(conn, standard, slots=("azure",), estimate=1, total=100)


def test_azure_only_run_is_refused_when_estimate_does_not_fit(path: str) -> None:
    run_id = _run(path)
    with transaction(path) as conn, pytest.raises(ProviderAdmissionError):
        admit_route(conn, run_id, slots=("azure",), estimate=101, total=100)


def test_physical_reserve_converts_forecast_and_settlement_restores_unused_part(path: str) -> None:
    run_id = _run(path)
    with transaction(path) as conn:
        admit_route(conn, run_id, slots=("azure",), estimate=90, total=100)
        reserve_spend(conn, "call", _quote(run_id))
        assert conn.execute("SELECT forecast_microeur FROM llm_routes").fetchone()[0] == 30
        assert conn.execute("SELECT charged_microeur FROM llm_spend").fetchone()[0] == 60
        settle_spend(conn, "call", 10, 10, 10, 0, 0)
        assert conn.execute("SELECT forecast_microeur FROM llm_routes").fetchone()[0] == 80
        settle_spend(conn, "call", 0, 0, 0, 0, 0)
        assert conn.execute("SELECT forecast_microeur FROM llm_routes").fetchone()[0] == 80


def test_concurrent_calls_and_forecasts_never_cross_total(path: str) -> None:
    run_id = _run(path)
    with transaction(path) as conn:
        admit_route(conn, run_id, slots=("azure",), estimate=90, total=100)

    def reserve(index: int) -> bool:
        try:
            with transaction(path, durable=True) as conn:
                reserve_spend(conn, str(index), _quote(run_id))
            return True
        except ProviderAdmissionError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 1
    with connect(path) as conn:
        money = conn.execute("SELECT SUM(charged_microeur) FROM llm_spend").fetchone()[0]
        forecast = conn.execute("SELECT SUM(forecast_microeur) FROM llm_routes").fetchone()[0]
        assert money + forecast == 90


@pytest.mark.parametrize("action", ["terminal", "delete"])
def test_terminal_or_deleted_run_releases_only_forecast_and_keeps_unknown_money(
    path: str, action: str
) -> None:
    run_id = _run(path)
    with transaction(path) as conn:
        admit_route(conn, run_id, slots=("azure",), estimate=90, total=100)
        reserve_spend(conn, "unknown", _quote(run_id))
    if action == "terminal":
        runs.update_run_status(run_id, RunStatus.CANCELLED, db_path=path)
    else:
        runs.delete_run(run_id)
    with connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_forecast_allocations").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COALESCE(SUM(forecast_microeur),0) FROM llm_routes").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT charged_microeur FROM llm_spend").fetchone()[0] == 60
        settle_spend(conn, "unknown", 10, 10, 10, 0, 0)
        assert (
            conn.execute("SELECT COALESCE(SUM(forecast_microeur),0) FROM llm_routes").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("path_taken", ["task_failure", "restart", "startup_heal"])
def test_failed_run_releases_its_forecast_on_every_failure_path(path: str, path_taken: str) -> None:
    from co_scientist.orchestration.repository import runs_views

    run_id, other = _run(path), _run(path)
    with transaction(path) as conn:
        admit_route(conn, run_id, slots=("azure",), estimate=90, total=100)
        assert not admit_route(
            conn, other, slots=("anthropic", "azure"), estimate=90, total=100
        ).azure_allowed
    runs.update_run_status(run_id, RunStatus.RUNNING, db_path=path)
    if path_taken == "task_failure":
        with transaction(path) as conn:
            runs_views._settle_run_for_failed_task(
                conn, run_id, "engine.generate", "provider rejected", retryable=False
            )
    elif path_taken == "restart":
        assert runs_views.reconcile_interrupted_runs(path)["failed"] == [run_id]
    else:
        with transaction(path) as conn:
            conn.execute("UPDATE runs SET status='failed' WHERE id=?", (run_id,))
        runs_views.reconcile_interrupted_runs(path)
    with transaction(path) as conn:
        run = runs.get_run(run_id, conn=conn)
        assert run is not None and run.status == RunStatus.FAILED.value
        assert (
            conn.execute(
                "SELECT forecast_microeur FROM llm_routes WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )
        conn.execute("DELETE FROM llm_routes WHERE run_id=?", (other,))
        assert admit_route(conn, other, slots=("azure",), estimate=90, total=100).azure_allowed


def test_free_slot_cap_is_atomic_and_renews_next_utc_day(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.db import llm_routes

    monkeypatch.setattr(llm_routes, "current_time", lambda: 200000.0)
    with transaction(path) as conn:
        reserve_free_call(conn, 1)
        with pytest.raises(OpenRouterCapacityError):
            reserve_free_call(conn, 1)
    assert not free_available(path, 1)
    monkeypatch.setattr(llm_routes, "current_time", lambda: 300000.0)
    assert free_available(path, 1)


@pytest.fixture
def providers(path: str, monkeypatch: pytest.MonkeyPatch) -> str:
    for name, value in {
        "LLM_ENABLED": "true",
        "LLM_AZURE_ENABLED": "true",
        "LLM_TOTAL_BUDGET_EUR": "1",
        "LLM_AZURE_EXPIRES_AT": "2099-01-04T00:00:00+00:00",
        "LLM_USD_TO_EUR": "0.88",
        "OPENROUTER_API_KEY": "fake-router",
        "ANTHROPIC_API_KEY": "fake-subscriber",
        "AZURE_OPENAI_API_KEY": "fake-azure",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "azure-deployment",
    }.items():
        monkeypatch.setenv(name, value)
    record_azure_allowance(path)
    run_id = _run(path)
    with transaction(path) as conn:
        admit_route(
            conn, run_id, slots=("openrouter", "anthropic", "azure"), estimate=900000, total=1000000
        )
    return run_id


def _azure_response() -> dict[str, Any]:
    return {
        "id": "response-one",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "azure-deployment",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Azure answer", "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 3,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }


def _message(*, refused: bool) -> dict[str, Any]:
    return {
        "id": "test-message",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "stop_reason": "refusal" if refused else "end_turn",
        "stop_sequence": None,
        "content": [{"type": "text", "text": "declined text" if refused else "Subscriber answer"}],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


def test_native_sdk_header_metadata_stays_local_from_a_cold_cache(
    providers: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litellm import anthropic_beta_headers_manager as beta_headers

    requested: list[str] = []

    def unexpected_metadata(url: str, **kwargs: Any) -> httpx.Response:
        requested.append(url)
        raise RuntimeError("Metadata network access is unavailable")

    monkeypatch.setattr(beta_headers, "_BETA_HEADERS_CONFIG", None)
    monkeypatch.setattr(httpx, "get", unexpected_metadata)
    loaded = beta_headers.reload_beta_headers_config()
    assert loaded["anthropic"]
    assert loaded == beta_headers.GetAnthropicBetaHeadersConfig.load_local_beta_headers_config()
    assert requested == []


def _no_writer(path: str) -> None:
    with connect(path) as conn:
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")


async def test_real_sdk_order_and_refusal_falls_back_for_only_that_call(
    path: str, providers: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.core.config import DEFAULT_MODEL
    from co_scientist.platform.llm.admission.service import scoped_client
    from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
    from co_scientist.platform.llm.request.transport import complete_request
    from co_scientist.platform.llm.roles import scoped_call_policy
    from co_scientist.platform.llm.routing import scoped_operator_routing

    sent: list[tuple[str, dict[str, Any]]] = []
    messages = 0

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        nonlocal messages
        _no_writer(path)
        if request.url.host == "openrouter.ai":
            sent.append(("openrouter", json.loads(request.content)))
            return httpx.Response(
                429, request=request, json={"error": {"message": "quota exhausted", "code": 429}}
            )
        assert request.url.host == "api.anthropic.com"
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, request=request, json={"input_tokens": 20})
        messages += 1
        sent.append(("anthropic", json.loads(request.content)))
        return httpx.Response(200, request=request, json=_message(refused=messages == 1))

    def send_sync(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        _no_writer(path)
        assert request.url.host == "test.openai.azure.com"
        assert request.url.path == "/openai/v1/responses"
        sent.append(("azure", json.loads(request.content)))
        return httpx.Response(200, request=request, json=_azure_response())

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(httpx.Client, "send", send_sync)
    request = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "goal"}],
        "max_tokens": 1000,
    }
    with (
        using_backend(LitellmBackend()),
        scoped_client("owner", db_path=path),
        scoped_operator_routing(providers),
        scoped_call_policy("safety"),
    ):
        answer = await complete_request(request, DEFAULT_MODEL, byok=False, timeout_seconds=5)
        assert answer.choices[0].message.content == "Azure answer"
        another = await complete_request(request, DEFAULT_MODEL, byok=False, timeout_seconds=5)
        assert another.choices[0].message.content == "Subscriber answer"
    assert [slot for slot, _ in sent] == ["openrouter", "anthropic", "azure", "anthropic"]
    assert sent[1][1]["output_config"] == {"effort": "low"}
    assert sent[2][1]["reasoning"] == {"effort": "low"}
    assert sent[2][1]["model"] == "azure-deployment"
    assert "extra_body" not in sent[2][1]
    with connect(path) as conn:
        assert conn.execute("SELECT slot FROM llm_routes").fetchone()[0] == "anthropic"
        assert conn.execute("SELECT SUM(charged_microusd) FROM anthropic_credit").fetchone()[0] == 4
        assert conn.execute("SELECT SUM(charged_microeur) FROM llm_spend").fetchone()[0] == 4
        assert [
            row[0]
            for row in conn.execute("SELECT refused FROM anthropic_credit ORDER BY created_at")
        ] == [1, 0]
        assert conn.execute("SELECT refused FROM llm_spend").fetchone()[0] == 0
        history = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload_json FROM run_events WHERE type='model_provider' ORDER BY seq"
            )
        ]
        assert [(row["from"], row["to"]) for row in history] == [
            ("none", "openrouter"),
            ("openrouter", "anthropic"),
            ("anthropic", "azure"),
        ]


async def test_exhausted_slot_skips_immediately_and_stays_disabled_on_next_call(
    path: str, providers: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.core.config import DEFAULT_MODEL
    from co_scientist.platform.llm.admission.service import scoped_client
    from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
    from co_scientist.platform.llm.request.transport import complete_request
    from co_scientist.platform.llm.routing import scoped_operator_routing

    monkeypatch.delenv("OPENROUTER_API_KEY")
    with connect(path) as conn:
        conn.execute("UPDATE llm_routes SET slot='anthropic'")
    sent: list[str] = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(request.url.path)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, request=request, json={"input_tokens": 20})
        return httpx.Response(
            400,
            request=request,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low to access the Anthropic API",
                }
            },
        )

    def send_sync(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(200, request=request, json=_azure_response())

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(httpx.Client, "send", send_sync)
    request = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "goal"}],
        "max_tokens": 1000,
    }
    with (
        using_backend(LitellmBackend()),
        scoped_client("owner", db_path=path),
        scoped_operator_routing(providers),
    ):
        for _ in range(2):
            answer = await complete_request(request, DEFAULT_MODEL, byok=False, timeout_seconds=5)
            assert answer.choices[0].message.content == "Azure answer"
    assert sent == [
        "/v1/messages/count_tokens",
        "/v1/messages",
        "/openai/v1/responses",
        "/openai/v1/responses",
    ]
    with connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM anthropic_credit_state").fetchone()[0] == 1
        assert conn.execute("SELECT settled FROM anthropic_credit").fetchone()[0] == 0


async def test_long_prompt_moves_one_call_to_luna_and_the_next_returns_to_haiku(
    path: str, providers: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.core.config import DEFAULT_MODEL
    from co_scientist.platform.llm.admission.service import scoped_client
    from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
    from co_scientist.platform.llm.request.transport import complete_request
    from co_scientist.platform.llm.routing import scoped_operator_routing

    monkeypatch.delenv("OPENROUTER_API_KEY")
    with connect(path) as conn:
        conn.execute("UPDATE llm_routes SET slot='anthropic'")
    counts = iter((100_001, 20))
    sent: list[str] = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        sent.append(request.url.path)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, request=request, json={"input_tokens": next(counts)})
        return httpx.Response(200, request=request, json=_message(refused=False))

    def send_sync(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(200, request=request, json=_azure_response())

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(httpx.Client, "send", send_sync)
    request = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "goal"}],
        "max_tokens": 1000,
    }
    with (
        using_backend(LitellmBackend()),
        scoped_client("owner", db_path=path),
        scoped_operator_routing(providers),
    ):
        long = await complete_request(request, DEFAULT_MODEL, byok=False, timeout_seconds=5)
        short = await complete_request(request, DEFAULT_MODEL, byok=False, timeout_seconds=5)
    assert long.choices[0].message.content == "Azure answer"
    assert short.choices[0].message.content == "Subscriber answer"
    assert sent == [
        "/v1/messages/count_tokens",
        "/openai/v1/responses",
        "/v1/messages/count_tokens",
        "/v1/messages",
    ]
    with connect(path) as conn:
        assert conn.execute("SELECT slot FROM llm_routes").fetchone()[0] == "anthropic"
        reasons = [
            json.loads(row[0])["reason"]
            for row in conn.execute(
                "SELECT payload_json FROM run_events WHERE type='model_provider' ORDER BY seq"
            )
        ]
        assert reasons[-1] == "long_context"


@pytest.mark.parametrize("cause", ["prompt_cap", "output_cap", "credit_cap"])
async def test_native_claude_capacity_failover_keeps_low_effort(
    path: str, providers: str, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    from co_scientist.core.config import DEFAULT_MODEL
    from co_scientist.platform.llm.admission.anthropic import credit_config, credit_rates
    from co_scientist.platform.llm.admission.service import scoped_client
    from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
    from co_scientist.platform.llm.request.transport import complete_request
    from co_scientist.platform.llm.routing import scoped_operator_routing

    monkeypatch.delenv("OPENROUTER_API_KEY")
    if cause == "credit_cap":
        config = credit_config()
        with transaction(path) as conn:
            conn.execute(
                "INSERT INTO anthropic_credit "
                "(id,cycle_start,created_at,role,reserved_microusd,charged_microusd,"
                "settled,input_bound,output_bound,rates) VALUES (?,?,?,?,?,?,1,0,0,?)",
                (
                    "previous",
                    config.cycle_start,
                    config.cycle_start,
                    "worker",
                    config.limit,
                    config.limit,
                    credit_rates(),
                ),
            )
    sent: list[str] = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        _no_writer(path)
        sent.append(request.url.path)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(
                200, request=request, json={"input_tokens": 100001 if cause == "prompt_cap" else 20}
            )
        body = json.loads(request.content)
        assert body["output_config"] == {"effort": "low"}
        assert body["max_tokens"] == 16384
        response = _message(refused=False)
        response["stop_reason"] = "max_tokens"
        return httpx.Response(200, request=request, json=response)

    def send_sync(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        _no_writer(path)
        sent.append(request.url.path)
        return httpx.Response(200, request=request, json=_azure_response())

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(httpx.Client, "send", send_sync)
    request = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "goal"}],
        "max_tokens": 20000,
    }
    with (
        using_backend(LitellmBackend()),
        scoped_client("owner", db_path=path),
        scoped_operator_routing(providers),
    ):
        response = await complete_request(request, DEFAULT_MODEL, byok=False, timeout_seconds=5)
    assert response.choices[0].message.content == "Azure answer"
    assert sent[-1] == "/openai/v1/responses"
    assert sent.count("/v1/messages") == int(cause == "output_cap")
    assert sent.count("/v1/messages/count_tokens") == int(cause != "credit_cap")


@pytest.mark.parametrize(
    "cause", ["kill", "azure_kill", "expired", "total_spent", "provider_error"]
)
async def test_azure_terminal_guard_never_calls_a_later_provider(
    path: str, providers: str, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    from co_scientist.core.config import DEFAULT_MODEL
    from co_scientist.platform.llm.admission.service import scoped_client
    from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
    from co_scientist.platform.llm.request.transport import complete_request
    from co_scientist.platform.llm.routing import scoped_operator_routing

    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    if cause in ("kill", "azure_kill"):
        monkeypatch.setenv("LLM_ENABLED" if cause == "kill" else "LLM_AZURE_ENABLED", "false")
    elif cause == "expired":
        monkeypatch.setenv("LLM_AZURE_EXPIRES_AT", "2020-01-04T00:00:00+00:00")
    elif cause == "total_spent":
        monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "0.000001")
    sent: list[str] = []

    def send(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(
            500, request=request, json={"error": {"message": "synthetic failure"}}
        )

    monkeypatch.setattr(httpx.Client, "send", send)
    request = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "goal"}],
        "max_tokens": 1000,
    }
    with (
        using_backend(LitellmBackend()),
        scoped_client("owner", db_path=path),
        scoped_operator_routing(providers),
        pytest.raises(ProviderAdmissionError, match="No model is available right now"),
    ):
        await complete_request(request, DEFAULT_MODEL, byok=False, timeout_seconds=5)
    assert len(sent) == int(cause == "provider_error")


@pytest.mark.parametrize("slot", ["anthropic", "azure"])
def test_native_credit_preflight_works_without_openrouter_and_notice_keeps_usable_slot(
    path: str,
    providers: str,
    monkeypatch: pytest.MonkeyPatch,
    slot: str,
) -> None:
    from co_scientist.api.launch_control_api import router
    from co_scientist.core.config import DEFAULT_MODEL
    from co_scientist.platform.llm import process_mode
    from co_scientist.platform.llm.admission.service import scoped_client
    from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.delenv("COSCIENTIST_FORCE_OFFLINE", raising=False)
    monkeypatch.delenv("COSCIENTIST_TEST_DOUBLE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    if slot == "anthropic":
        monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "0.000001")
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY")
    app = FastAPI()
    app.include_router(router)
    previous = process_mode.install(process_mode.EnvProcessMode())
    try:
        with using_backend(LitellmBackend()), scoped_client("owner", db_path=path):
            assert process_mode.credential_available(DEFAULT_MODEL)
            with TestClient(app) as client:
                notice = client.get("/api/launch-status", headers={"X-Client-ID": "owner"}).json()
            if slot == "anthropic":
                assert notice["free_runs_allowed"] and notice["reason"] is None
            else:
                # The existing run's forecast leaves usable Azure capacity.
                assert notice["free_runs_allowed"]
    finally:
        process_mode.install(previous)


async def test_goal_text_on_claude_sends_no_thinking_and_only_its_answer_budget(
    path: str, providers: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.core.config import settings
    from co_scientist.domains.chat.goal_text import generate_goal_restatement, generate_run_title
    from co_scientist.platform.llm.admission.service import scoped_client
    from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
    from co_scientist.platform.llm.routing import scoped_operator_routing

    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.delenv("COSCIENTIST_TEST_DOUBLE", raising=False)
    monkeypatch.setattr(settings, "chat_model_name", "anthropic/claude-haiku-5-5")
    with connect(path) as conn:
        conn.execute("UPDATE llm_routes SET slot='anthropic'")
    bodies: list[dict[str, Any]] = []

    async def send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, request=request, json={"input_tokens": 60})
        body = json.loads(request.content)
        bodies.append(body)
        response = _message(refused=False)
        # max_tokens bounds thinking and answer together; a budget under one
        # thought is spent before any text.
        if body["thinking"] != {"type": "disabled"} and body["max_tokens"] < 1024:
            response["stop_reason"] = "max_tokens"
            response["content"] = [{"type": "thinking", "thinking": "", "signature": "s"}]
            response["usage"]["output_tokens"] = body["max_tokens"]
        else:
            text = "A restatement." if len(bodies) > 1 else "Ferroptosis Targets"
            response["content"] = [{"type": "text", "text": text}]
        return httpx.Response(200, request=request, json=response)

    def azure(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        raise AssertionError("goal text fell back to Azure")

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(httpx.Client, "send", azure)
    goal = "Propose experiments testing whether ferroptosis regulators can be targeted."
    with (
        using_backend(LitellmBackend()),
        scoped_client("owner", db_path=path),
        scoped_operator_routing(providers),
    ):
        title = await generate_run_title(goal)
        restatement = await generate_goal_restatement(goal)
    assert (title, restatement) == ("Ferroptosis Targets", "A restatement.")
    assert [body["max_tokens"] for body in bodies] == [40, 260]
    assert all(body["thinking"] == {"type": "disabled"} for body in bodies)
    assert all(body["output_config"] == {"effort": "low"} for body in bodies)
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT role,COUNT(refused),SUM(refused) FROM anthropic_credit GROUP BY role"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("goal_text", 2, 0)]
