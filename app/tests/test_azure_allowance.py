from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from co_scientist.core.exceptions import LLMTimeoutError, ProviderAdmissionError
from co_scientist.platform.db import connect, transaction
from co_scientist.platform.db.admission import ProviderReservation
from co_scientist.platform.db.spend import active_allowance
from co_scientist.platform.llm.admission import spend as spend_policy
from co_scientist.platform.llm.admission.service import reserve_physical, settle_physical
from co_scientist.platform.llm.admission.spend import azure_dispatch_permit, establish_allowance
from co_scientist.platform.llm.request.azure import LUNA, AzureResponsesBackend
from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
from co_scientist.platform.llm.request.transport import complete_request

from tests._client import make_client, make_operator_client

EXPIRES = "2099-01-04T00:00:00+00:00"
DEPLOYMENTS = {LUNA: "luna-deployment"}


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    path = str(tmp_path / "azure.db")
    for name, value in {
        "COSCIENTIST_DB_PATH": path,
        "LLM_ENABLED": "true",
        "LLM_AZURE_ENABLED": "true",
        "LLM_TOTAL_BUDGET_EUR": "1",
        "LLM_AZURE_EXPIRES_AT": EXPIRES,
        "LLM_AZURE_CUTOFF_HOURS": "48",
        "LLM_USD_TO_EUR": "0.88",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LLM_AZURE_UNTIL", raising=False)
    _anchor(path, "1")
    return path


def _anchor(path: str, allowance: str, *, expires: str = EXPIRES, hours: str = "48") -> None:
    establish_allowance(
        path,
        grant_eur=Decimal(allowance) + Decimal("0.5"),
        prior_usage_eur=Decimal("0.25"),
        buffer_eur=Decimal("0.25"),
        expires_at=datetime.fromisoformat(expires),
        cutoff_hours=Decimal(hours),
        usd_to_eur=Decimal("0.88"),
    )


def _request(model: str = LUNA, **changes: Any) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "answer"}],
        "max_tokens": 1000,
        **changes,
    }


def _usage(**changes: Any) -> Any:
    return SimpleNamespace(
        usage={
            "prompt_tokens": 90,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            **changes,
        }
    )


def _spent(path: str) -> int:
    with connect(path) as conn:
        return int(
            conn.execute("SELECT COALESCE(SUM(charged_microeur),0) FROM llm_spend").fetchone()[0]
        )


def _rows(path: str) -> list[sqlite3.Row]:
    with connect(path) as conn:
        return list(conn.execute("SELECT * FROM llm_spend ORDER BY created_at"))


def _set_clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    for module in (
        "co_scientist.platform.llm.admission.spend",
        "co_scientist.platform.db.spend",
    ):
        monkeypatch.setattr(f"{module}.current_time", lambda: moment.timestamp())


class _Fake:
    def __init__(self, answer: Any = None, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer = answer if answer is not None else _usage()
        self.error = error

    async def complete(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.answer

    def supports_json_schema(self, model_name: str) -> bool:
        return True


def _response_body(**changes: Any) -> dict[str, Any]:
    return {
        "id": "response-one",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "deployment",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 90,
            "output_tokens": 30,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 20},
        },
        **changes,
    }


def _native(
    seen: list[httpx.Request], deployments: dict[str, str] = DEPLOYMENTS
) -> AzureResponsesBackend:
    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_response_body())

    client = openai.OpenAI(
        api_key="fake",
        base_url="https://test.openai.azure.com/openai/v1/",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    return AzureResponsesBackend(client, deployments)


# Persistence: one lifetime allowance that nothing resets.


def test_fresh_database_without_operator_allowance_never_dispatches(
    ledger: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A lost volume, a benchmark runner or a laptop with production keys starts
    # from an empty store; it must not inherit a full allowance from settings.
    monkeypatch.setenv("COSCIENTIST_DB_PATH", str(tmp_path / "fresh.db"))
    fake = _Fake()
    with using_backend(fake), pytest.raises(ProviderAdmissionError, match="No model"):
        asyncio.run(complete_request(_request(), LUNA, byok=False, timeout_seconds=5))
    assert fake.calls == []


def test_raising_the_setting_cannot_exceed_the_recorded_allowance(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _anchor(ledger, "0.004")
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "1000")
    accepted = 0
    for _ in range(10):
        try:
            reserve_physical(_request())
            accepted += 1
        except ProviderAdmissionError:
            break
    assert accepted == 3 and _spent(ledger) <= 4000


def test_lowering_the_setting_takes_effect_without_touching_the_record(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "0.0001")
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())
    assert _spent(ledger) == 0


def test_a_new_allowance_version_never_forgets_earlier_spend(ledger: str) -> None:
    reserve_physical(_request())
    first = _spent(ledger)
    _anchor(ledger, str(Decimal(first) / 1_000_000))
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())
    assert _spent(ledger) == first


def test_ledger_and_allowance_history_are_append_only(ledger: str) -> None:
    receipt = reserve_physical(_request())
    settle_physical(receipt, _usage())
    statements = (
        "DELETE FROM llm_spend",
        "UPDATE llm_spend SET charged_microeur=0",
        "UPDATE llm_spend SET settled=0",
        "DELETE FROM llm_azure_allowance",
        "UPDATE llm_azure_allowance SET allowance_microeur=allowance_microeur*10",
        "DELETE FROM llm_spend_holds",
    )
    with transaction(ledger) as conn:
        conn.execute("INSERT INTO llm_spend_holds VALUES ('test')")
    for statement in statements:
        with pytest.raises(sqlite3.DatabaseError), transaction(ledger) as conn:
            conn.execute(statement)
    assert _spent(ledger) == 22


def test_unsettled_reservation_and_allowance_survive_abrupt_exit(ledger: str) -> None:
    env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH") if key in os.environ}
    env.update(
        {
            "COSCIENTIST_DB_PATH": ledger,
            "LLM_ENABLED": "true",
            "LLM_AZURE_ENABLED": "true",
            "LLM_TOTAL_BUDGET_EUR": "1",
            "LLM_AZURE_EXPIRES_AT": EXPIRES,
            "LLM_USD_TO_EUR": "0.88",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "PYTHON_DOTENV_DISABLED": "1",
        }
    )
    script = """
import os
from co_scientist.platform.llm.admission.service import reserve_physical
reserve_physical({
    "model": "azure/gpt-6-luna-2026-09-22",
    "messages": [{"role": "user", "content": "answer"}],
    "max_tokens": 1000,
})
os._exit(0)
"""
    for _ in range(2):
        subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
    rows = _rows(ledger)
    assert len(rows) == 2 and all(not row["settled"] for row in rows)
    assert _spent(ledger) == sum(row["reserved_microeur"] for row in rows)
    with connect(ledger) as conn:
        allowance = active_allowance(conn)
    assert allowance is not None and allowance.allowance == 1_000_000


# Concurrency: the reservation and the remaining-allowance check are one writer.


def test_concurrent_dispatch_exhausts_the_allowance_exactly_once(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _anchor(ledger, "0.004")
    # Hung calls keep their whole reservation, so concurrency cannot reuse it.
    fake = _Fake(error=LLMTimeoutError("no answer"))

    def call(_: int) -> str:
        try:
            asyncio.run(complete_request(_request(), LUNA, byok=False, timeout_seconds=5))
        except ProviderAdmissionError:
            return "refused"
        except LLMTimeoutError:
            return "dispatched"
        return "answered"

    with using_backend(fake), concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(call, range(48)))
    rows = _rows(ledger)
    assert results.count("dispatched") == len(fake.calls) == len(rows) >= 1
    assert results.count("refused") == 48 - len(rows)
    assert _spent(ledger) <= 4000 < _spent(ledger) + rows[0]["reserved_microeur"]


# Retries: every physical attempt reserves its own upper bound.


async def test_each_engine_retry_reserves_and_failed_attempts_keep_their_charge(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.llm.call import call_llm
    from co_scientist.platform.llm.values import CompletionSpec

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    answers: list[Any] = [
        openai.InternalServerError(
            "busy",
            response=httpx.Response(500, request=httpx.Request("POST", "https://x")),
            body=None,
        ),
        openai.InternalServerError(
            "busy",
            response=httpx.Response(500, request=httpx.Request("POST", "https://x")),
            body=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None), finish_reason="stop"
                )
            ],
            usage=_usage().usage,
            model=LUNA,
        ),
    ]
    calls = []

    class Scripted(_Fake):
        async def complete(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

    with using_backend(Scripted()):
        assert await call_llm("question", CompletionSpec(LUNA, max_tokens=1000)) == "done"
    rows = _rows(ledger)
    assert len(calls) == len(rows) == 3
    assert [bool(row["settled"]) for row in rows] == [False, False, True]
    assert all(row["charged_microeur"] == row["reserved_microeur"] for row in rows[:2])


def test_sdk_clients_are_built_without_retries_or_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "fake",
        "AZURE_OPENAI_DEPLOYMENT": "one",
    }.items():
        monkeypatch.setenv(name, value)
    backend = AzureResponsesBackend.from_environment()
    try:
        client = backend._client
        assert client.max_retries == 0
        assert client._client.follow_redirects is False
    finally:
        backend.close()


# Settlement: only complete, validated usage lowers a charge.


def test_complete_usage_settles_once_and_never_below_zero_or_twice(ledger: str) -> None:
    receipt = reserve_physical(_request())
    reserved = _spent(ledger)
    settle_physical(receipt, _usage())
    settled = _spent(ledger)
    assert 0 < settled < reserved
    settle_physical(receipt, _usage(prompt_tokens=1, completion_tokens=1))
    assert _spent(ledger) == settled


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"prompt_tokens": 90},
        {"prompt_tokens": "90", "completion_tokens": 30},
        {"prompt_tokens": -1, "completion_tokens": 30},
        {"prompt_tokens": True, "completion_tokens": 30},
    ],
)
def test_missing_or_malformed_usage_keeps_the_full_reservation(
    ledger: str, usage: dict[str, Any] | None
) -> None:
    receipt = reserve_physical(_request())
    reserved = _spent(ledger)
    settle_physical(receipt, SimpleNamespace(usage=usage))
    assert _spent(ledger) == reserved


@pytest.mark.parametrize(
    "usage",
    [
        {"completion_tokens": 1001},
        {"prompt_tokens": 10**9},
        {"prompt_tokens_details": {"cached_tokens": 91, "cache_write_tokens": 0}},
        {"completion_tokens_details": {"reasoning_tokens": 31}},
    ],
)
def test_inconsistent_usage_keeps_charge_and_stops_all_paid_calls(
    ledger: str, usage: dict[str, Any]
) -> None:
    receipt = reserve_physical(_request())
    reserved = _spent(ledger)
    with pytest.raises(ProviderAdmissionError):
        settle_physical(receipt, _usage(**usage))
    assert _spent(ledger) == reserved
    with connect(ledger) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_spend_holds").fetchone()[0] == 1
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


async def test_timeout_keeps_the_full_reservation(ledger: str) -> None:
    with (
        using_backend(_Fake(error=LLMTimeoutError("no answer"))),
        pytest.raises(LLMTimeoutError),
    ):
        await complete_request(_request(), LUNA, byok=False, timeout_seconds=5)
    [row] = _rows(ledger)
    assert not row["settled"] and row["charged_microeur"] == row["reserved_microeur"]


async def test_interrupted_and_usage_free_streams_keep_the_full_reservation(
    ledger: str,
) -> None:
    class Stream:
        def __init__(self, usage: bool) -> None:
            self.chunks = [SimpleNamespace(choices=[], usage=None)] * 3
            if usage:
                self.chunks.append(SimpleNamespace(choices=[], usage=_usage().usage))

        def __aiter__(self) -> Stream:
            return self

        async def __anext__(self) -> Any:
            if not self.chunks:
                raise StopAsyncIteration
            return self.chunks.pop(0)

        async def aclose(self) -> None:
            self.chunks = []

    for usage, consume in ((True, 1), (False, 99)):
        with using_backend(_Fake(answer=Stream(usage))):
            stream = await complete_request(
                _request(stream=True), LUNA, byok=False, timeout_seconds=5
            )
            seen = 0
            async for _ in stream:
                seen += 1
                if seen == consume:
                    await stream.aclose()
                    break
    rows = _rows(ledger)
    assert len(rows) == 2
    assert all(
        not row["settled"] and row["charged_microeur"] == row["reserved_microeur"] for row in rows
    )


# Expiry: an exact UTC instant, an earlier cutoff, and no automatic extension.


@pytest.mark.parametrize(
    "offset,allowed",
    [(timedelta(hours=-48, seconds=-1), True), (timedelta(hours=-48), False), (timedelta(), False)],
)
def test_cutoff_precedes_the_exact_expiry_by_the_configured_margin(
    ledger: str, monkeypatch: pytest.MonkeyPatch, offset: timedelta, allowed: bool
) -> None:
    _set_clock(monkeypatch, datetime.fromisoformat(EXPIRES) + offset)
    if allowed:
        # A record is only trusted for a bounded time, so record it "then".
        _anchor(ledger, "1")
        reserve_physical(_request())
    else:
        with pytest.raises(ProviderAdmissionError):
            reserve_physical(_request())
        assert _spent(ledger) == 0


@pytest.mark.parametrize(
    "name,value",
    [
        ("LLM_AZURE_EXPIRES_AT", ""),
        ("LLM_AZURE_EXPIRES_AT", "2099-01-04"),
        ("LLM_AZURE_EXPIRES_AT", "2099-01-04T00:00:00"),
        ("LLM_AZURE_EXPIRES_AT", "not a time"),
        ("LLM_AZURE_CUTOFF_HOURS", "0.5"),
        ("LLM_AZURE_CUTOFF_HOURS", "nan"),
        ("LLM_AZURE_CUTOFF_HOURS", "-48"),
    ],
)
def test_missing_ambiguous_or_unsafe_expiry_settings_fail_closed(
    ledger: str, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())
    assert _spent(ledger) == 0


def test_retired_date_only_expiry_alone_does_not_enable_azure(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_AZURE_EXPIRES_AT")
    monkeypatch.setenv("LLM_AZURE_UNTIL", "2099-01-04")
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


def test_later_expiry_in_settings_cannot_extend_the_recorded_cutoff(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = "2030-01-04T00:00:00+00:00"
    _anchor(ledger, "1", expires=recorded)
    _set_clock(monkeypatch, datetime.fromisoformat(recorded) - timedelta(hours=47))
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


async def test_permit_expires_with_the_cutoff_even_after_reservation(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []
    backend = _native(seen)
    cutoff = datetime.fromisoformat(EXPIRES) - timedelta(hours=48)

    async def after_cutoff(function: Any, *args: Any) -> Any:
        _set_clock(monkeypatch, cutoff)
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", after_cutoff)
    try:
        with using_backend(backend), pytest.raises(ProviderAdmissionError):
            await complete_request(_request(), LUNA, byok=False, timeout_seconds=5)
    finally:
        backend.close()
    assert seen == [] and _spent(ledger) > 0


# Fail closed on pricing and request shapes the bound does not cover.


def test_code_price_below_the_verified_price_refuses_dispatch(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.llm import profile

    original = profile.model_profile(LUNA)
    assert original.price is not None and original.price.long_context is not None
    long_context = replace(original.price.long_context, completion_usd_per_million=0.01)
    cheaper = replace(original, price=replace(original.price, long_context=long_context))
    monkeypatch.setattr(
        spend_policy, "model_profile", lambda name: cheaper if name == LUNA else original
    )
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())
    assert _spent(ledger) == 0


def test_lower_exchange_rate_than_verified_refuses_dispatch(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_USD_TO_EUR", "0.5")
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


@pytest.mark.parametrize(
    "changes",
    [
        {"messages": [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "answer"}]}]},
        {"tools": [{"type": "web_search"}]},
        {"n": 2},
        {"max_tokens": None},
    ],
)
async def test_unsupported_request_shapes_are_refused_before_reservation(
    ledger: str, changes: dict[str, Any]
) -> None:
    fake = _Fake()
    with using_backend(fake), pytest.raises(ProviderAdmissionError):
        await complete_request(_request(**changes), LUNA, byok=False, timeout_seconds=5)
    assert fake.calls == [] and _spent(ledger) == 0


async def test_input_bound_counts_schema_and_tool_definitions(ledger: str) -> None:
    schema = {"type": "object", "description": "x" * 20_000}
    plain = _Fake()
    with using_backend(plain):
        await complete_request(_request(), LUNA, byok=False, timeout_seconds=5)
    with using_backend(_Fake()):
        await complete_request(
            _request(
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "r", "schema": schema},
                }
            ),
            LUNA,
            byok=False,
            timeout_seconds=5,
        )
    small, large = _rows(ledger)
    assert large["input_bound"] >= small["input_bound"] + 20_000


# Bypass: the native backend dispatches only under a fresh, matching permit.


async def test_native_backend_refuses_a_call_that_was_never_reserved(ledger: str) -> None:
    seen: list[httpx.Request] = []
    backend = _native(seen)
    try:
        with pytest.raises(ProviderAdmissionError):
            await backend.complete(**_request())
    finally:
        backend.close()
    assert seen == []


async def test_a_permit_covers_exactly_one_matching_request(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.llm import profile

    luna = profile.model_profile(LUNA)
    monkeypatch.setattr("co_scientist.platform.llm.request.azure.model_profile", lambda name: luna)
    seen: list[httpx.Request] = []
    backend = _native(seen, {**DEPLOYMENTS, "azure/other": "other-deployment"})
    receipt = reserve_physical(_request())
    try:
        with azure_dispatch_permit(receipt):
            with pytest.raises(ProviderAdmissionError):
                await backend.complete(**_request("azure/other"))
            with pytest.raises(ProviderAdmissionError):
                await backend.complete(**_request(max_tokens=1001))
            with pytest.raises(ProviderAdmissionError):
                await backend.complete(
                    **_request(messages=[{"role": "user", "content": "x" * 50_000}])
                )
            await backend.complete(**_request())
            with pytest.raises(ProviderAdmissionError):
                await backend.complete(**_request())
    finally:
        backend.close()
    assert len(seen) == 1


async def test_receipt_without_money_cannot_open_a_native_permit(ledger: str) -> None:
    seen: list[httpx.Request] = []
    backend = _native(seen)
    try:
        with (
            azure_dispatch_permit(ProviderReservation("forged", ledger)),
            pytest.raises(ProviderAdmissionError),
        ):
            await backend.complete(**_request())
    finally:
        backend.close()
    assert seen == []


@pytest.mark.parametrize("model", ["azure/gpt-4o", "azure_ai/some-model", "azure/responses/x"])
async def test_non_native_azure_routes_never_reach_the_sdk(
    ledger: str, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "operator-key")
    reached = []

    async def acompletion(**kwargs: Any) -> Any:
        reached.append(kwargs)
        return _usage()

    monkeypatch.setattr("litellm.acompletion", acompletion)
    for byok in (False, True):
        with using_backend(LitellmBackend()), pytest.raises(ProviderAdmissionError):
            await complete_request(
                _request(model, api_key="caller-key" if byok else None),
                model,
                byok=byok,
                timeout_seconds=5,
            )
    with pytest.raises(ProviderAdmissionError):
        await LitellmBackend().complete(**_request(model))
    assert reached == [] and _spent(ledger) == 0


async def test_full_native_path_reserves_dispatches_once_and_settles(ledger: str) -> None:
    seen: list[httpx.Request] = []
    backend = _native(seen)
    try:
        with using_backend(backend):
            await complete_request(_request(), LUNA, byok=False, timeout_seconds=5)
    finally:
        backend.close()
    [row] = _rows(ledger)
    assert len(seen) == 1 and row["settled"] and row["prompt_tokens"] == 90
    assert json.loads(seen[0].content)["max_output_tokens"] == 1000


def test_only_the_gateway_opens_permits_or_reads_the_operator_key() -> None:
    root = Path(__file__).resolve().parents[2] / "engine" / "src" / "co_scientist"
    permit_users, key_readers = set(), set()
    for source in root.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        relative = source.relative_to(root).as_posix()
        if "azure_dispatch_permit(" in text:
            permit_users.add(relative)
        if '"AZURE_OPENAI_API_KEY"' in text or "'AZURE_OPENAI_API_KEY'" in text:
            key_readers.add(relative)
        if "openai.OpenAI(" in text or "AzureOpenAI(" in text or "AsyncOpenAI(" in text:
            assert relative == "platform/llm/request/azure.py", relative
    assert permit_users == {
        "platform/llm/admission/spend.py",
        "platform/llm/request/transport.py",
    }
    # Readiness checks may test presence; only the native backend sends it.
    assert key_readers <= {
        "platform/llm/request/azure.py",
        "platform/llm/routing.py",
        "core/config.py",
    }


# Operator record: explicit, token-protected and computed conservatively.


def test_allowance_record_requires_the_operator_token(ledger: str) -> None:
    body = {
        "grant_eur": "175.99",
        "prior_usage_eur": "0",
        "buffer_eur": "25",
        "expires_at": "2099-01-03T10:00:00Z",
        "cutoff_hours": "48",
        "usd_to_eur": "1.00",
    }
    client = make_client()
    assert client.get("/api/spend/azure-allowance").status_code == 404
    assert client.post("/api/spend/azure-allowance", json=body).status_code == 404


def test_allowance_record_subtracts_prior_usage_and_buffer(ledger: str) -> None:
    client = make_operator_client()
    active = client.get("/api/spend/azure-allowance").json()["active"]["version"]
    refused = client.post(
        "/api/spend/azure-allowance",
        json={
            "grant_eur": "175.99",
            "prior_usage_eur": "3.50",
            "buffer_eur": "25",
            "expires_at": "2099-01-03T10:00:00Z",
            "usd_to_eur": "1.00",
        },
    )
    assert refused.status_code == 422 and "supersedes_version" in refused.text
    answer = client.post(
        "/api/spend/azure-allowance",
        json={
            "supersedes_version": active,
            "grant_eur": "175.99",
            "prior_usage_eur": "3.50",
            "buffer_eur": "25",
            "expires_at": "2099-01-03T10:00:00Z",
            "cutoff_hours": "48",
            "usd_to_eur": "1.00",
            "note": "sponsorship lot",
        },
    )
    assert answer.status_code == 200, answer.text
    data = answer.json()
    assert data["allowance_eur"] == "147.49"
    assert data["cutoff_at"] == "2099-01-01T10:00:00+00:00"
    assert set(data["rates"]["models"]) == {LUNA}
    view = client.get("/api/spend/azure-allowance").json()
    assert view["active"]["version"] == data["version"]
    assert view["ledger_charged_and_reserved_eur"] == "0.000000"


@pytest.mark.parametrize(
    "changes",
    [
        {"buffer_eur": "0"},
        {"prior_usage_eur": "-1"},
        {"buffer_eur": "200"},
        {"expires_at": "2099-01-03"},
        {"expires_at": "2000-01-03T10:00:00Z"},
        {"cutoff_hours": "0"},
        {"grant_eur": "nan"},
    ],
)
def test_allowance_record_refuses_unsafe_inputs(ledger: str, changes: dict[str, str]) -> None:
    body = {
        "grant_eur": "175.99",
        "prior_usage_eur": "0",
        "buffer_eur": "25",
        "expires_at": "2099-01-03T10:00:00Z",
        "cutoff_hours": "48",
        "usd_to_eur": "1.00",
        **changes,
    }
    with connect(ledger) as conn:
        before = active_allowance(conn)
    answer = make_operator_client().post("/api/spend/azure-allowance", json=body)
    assert answer.status_code == 422
    with connect(ledger) as conn:
        assert active_allowance(conn) == before


def test_spend_view_reports_the_effective_allowance(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _anchor(ledger, "0.5")
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "100")
    data = make_operator_client().get("/api/spend").json()
    assert data["azure"]["total_budget_eur"] == 0.5
    assert (
        data["azure"]["cutoff_at"]
        == (datetime.fromisoformat(EXPIRES) - timedelta(hours=48))
        .astimezone(timezone.utc)
        .isoformat()
    )


def test_store_applies_the_record_even_if_policy_admitted_a_larger_total(ledger: str) -> None:
    # The record can be lowered between policy preparation and the writer.
    from co_scientist.platform.db.spend import SpendReservation, reserve_spend

    rates = json.dumps({"input": "1", "output": "1", "cached": "1", "write": "1", "fx": "1"})
    far = datetime.fromisoformat(EXPIRES).timestamp() + 10**9
    with transaction(ledger) as conn:
        cutoff = reserve_spend(
            conn, "a", SpendReservation(LUNA, "worker", 500_000, 10**12, far, 1, 1, rates)
        )
    assert cutoff == datetime.fromisoformat(EXPIRES).timestamp() - 48 * 3600
    with pytest.raises(ProviderAdmissionError), transaction(ledger) as conn:
        reserve_spend(
            conn, "b", SpendReservation(LUNA, "worker", 500_001, 10**12, far, 1, 1, rates)
        )
    assert _spent(ledger) == 500_000


def _record(path: str, allowance: str, **changes: Any) -> None:
    establish_allowance(
        path,
        **{
            "grant_eur": Decimal(allowance) + 1,
            "prior_usage_eur": Decimal(0),
            "buffer_eur": Decimal(1),
            "expires_at": datetime.fromisoformat(EXPIRES),
            "cutoff_hours": Decimal(48),
            "usd_to_eur": Decimal("0.88"),
            **changes,
        },
    )


def _version(path: str) -> int:
    with connect(path) as conn:
        allowance = active_allowance(conn)
    assert allowance is not None
    return allowance.version


def test_exchange_rate_has_no_default(ledger: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_USD_TO_EUR")
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


def test_loosening_a_limit_must_name_the_version_it_replaces(ledger: str) -> None:
    active = _version(ledger)
    later = datetime.fromisoformat(EXPIRES) + timedelta(days=1)
    for changes in ({}, {"expires_at": later}, {"cutoff_hours": Decimal(24)}):
        with pytest.raises(ValueError, match="supersedes_version"):
            _record(ledger, "1" if changes else "2", **changes)
    with pytest.raises(ValueError, match="supersedes_version"):
        _record(ledger, "2", supersedes_version=active - 1)
    assert _version(ledger) == active
    _record(ledger, "0.5")
    _record(ledger, "2", supersedes_version=active + 1)
    assert _version(ledger) == active + 2


def test_hold_survives_until_a_new_record_acknowledges_it(ledger: str) -> None:
    receipt = reserve_physical(_request())
    with pytest.raises(ProviderAdmissionError):
        settle_physical(receipt, _usage(prompt_tokens=10**9))
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())
    _record(ledger, "0.5")
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())
    with pytest.raises(ValueError, match="supersedes_version"):
        _record(ledger, "0.5", acknowledge_holds=True)
    _record(ledger, "0.5", acknowledge_holds=True, supersedes_version=_version(ledger))
    reserve_physical(_request())
    with transaction(ledger) as conn:
        conn.execute("INSERT INTO llm_spend_holds VALUES ('later')")
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


def test_restore_marker_holds_azure_in_a_restored_store(ledger: str) -> None:
    env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH") if key in os.environ}
    env["PYTHON_DOTENV_DISABLED"] = "1"
    subprocess.run(
        [sys.executable, "-m", "co_scientist.platform.db.spend", ledger],
        env=env,
        check=True,
        timeout=60,
    )
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


def test_allowance_older_than_the_price_check_window_refuses(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.db.spend import ALLOWANCE_MAX_AGE_SECONDS

    with connect(ledger) as conn:
        allowance = active_allowance(conn)
    assert allowance is not None
    stale = allowance.created_at + ALLOWANCE_MAX_AGE_SECONDS + 1
    _set_clock(monkeypatch, datetime.fromtimestamp(stale, timezone.utc))
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


@pytest.mark.parametrize("hours", ["1e12", "9000", "inf"])
def test_out_of_range_cutoff_refuses_azure_without_breaking_routing(
    ledger: str, monkeypatch: pytest.MonkeyPatch, hours: str
) -> None:
    from co_scientist.platform.llm.routing import available_slots

    monkeypatch.setenv("LLM_AZURE_CUTOFF_HOURS", hours)
    for name in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    ):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setenv("COSCIENTIST_REQUIRE_FREE_MODELS", "false")
    assert "azure" not in available_slots(ledger).slots
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


async def test_replayed_reasoning_reserves_its_producing_output_cap(ledger: str) -> None:
    replay = {
        "role": "assistant",
        "content": None,
        "responses_items": [
            {"type": "reasoning", "id": "r", "summary": [], "encrypted_content": "x"}
        ],
    }
    with using_backend(_Fake()):
        await complete_request(_request(), LUNA, byok=False, timeout_seconds=5)
        await complete_request(
            _request(messages=[{"role": "user", "content": "answer"}, replay]),
            LUNA,
            byok=False,
            timeout_seconds=5,
        )
    plain, replayed = _rows(ledger)
    assert replayed["input_bound"] >= plain["input_bound"] + 1000
