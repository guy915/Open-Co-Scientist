from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from co_scientist.core.exceptions import LLMCallBudgetExceededError, LLMTimeoutError

from evaluations.quality_benchmark import BoundedBackend, collect


@pytest.mark.parametrize("ceiling,expected", [(2, 2), (1, 1)])
def test_sdk_connection_replay_consumes_http_allowance_before_sending(
    monkeypatch: pytest.MonkeyPatch,
    ceiling: int,
    expected: int,
) -> None:
    import httpx

    from evaluations import quality_benchmark
    from evaluations.benchmark_transport import count_http_attempts

    attempts = []

    def send(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.RemoteProtocolError("synthetic connection close", request=request)
        return httpx.Response(200, json={"synthetic": True}, request=request)

    async def request() -> None:
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        handler = AsyncHTTPHandler()
        await handler.client.aclose()
        handler.client = httpx.AsyncClient(transport=httpx.MockTransport(send))
        monkeypatch.setattr(
            handler,
            "create_client",
            lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(send)),
        )

        class Delegate(FakeBackend):
            async def complete(self, **completion_args: Any) -> Any:
                return await handler.post(
                    "https://openrouter.ai/api/v1/chat/completions", data="{}"
                )

        backend = quality_benchmark.BoundedBackend(Delegate(), ceiling)
        try:
            if ceiling == 1:
                with pytest.raises(LLMCallBudgetExceededError):
                    await backend.complete(model="fake")
            else:
                assert (await backend.complete(model="fake")).status_code == 200
            assert backend.calls == expected == len(attempts)
        finally:
            await handler.client.aclose()

    with count_http_attempts():
        asyncio.run(request())


def test_http_counter_and_receipt_marker_round_trip(tmp_path: Path) -> None:
    import sqlite3

    from evaluations._paired_db import read_snapshot
    from evaluations.benchmark_transport import COUNTER_SHA256, _read_counted_snapshot
    from evaluations.tests.test_paired_quality import fake_database

    db = tmp_path / "snapshot.db"
    fake_database(db, "cell-biology")
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE evaluation_runs "
            "(run_id, source_commit, physical_requests, request_ceiling)"
        )
        conn.execute("INSERT INTO evaluation_runs VALUES ('r',?,100,200)", ("a" * 40,))
    tagged = _read_counted_snapshot(db, "r", "cell-biology", "a" * 40)
    replayed = read_snapshot(db, "r", "cell-biology", "a" * 40)
    assert replayed == tagged
    assert replayed.metrics["physical_calls"] == 100
    assert replayed.metrics["call_count_basis"] == "http_transport_attempts"
    assert replayed.metrics["request_counter_sha256"] == COUNTER_SHA256
    copied = tmp_path / "snapshot-without-wal.db"
    copied.write_bytes(db.read_bytes())
    assert read_snapshot(copied, "r", "cell-biology", "a" * 40) == tagged


def test_concurrent_http_attempts_share_one_allowance() -> None:
    import httpx

    from evaluations import quality_benchmark
    from evaluations.benchmark_transport import count_http_attempts

    attempts = []

    def send(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(200, json={"synthetic": True}, request=request)

    class Delegate(FakeBackend):
        async def complete(self, **completion_args: Any) -> Any:
            async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
                return await client.post("https://openrouter.ai/api/v1/chat/completions")

    with count_http_attempts():
        backend = quality_benchmark.BoundedBackend(Delegate(), 5)

        def request(_: int) -> bool:
            try:
                asyncio.run(backend.complete(model="fake"))
                return True
            except LLMCallBudgetExceededError:
                return False

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(request, range(40)))
    assert sum(results) == backend.calls == len(attempts) == 5


def test_real_async_sdk_delegation_preserves_http_counter_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    import httpx

    from evaluations import quality_benchmark
    from evaluations.benchmark_transport import count_http_attempts

    def deny_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access during SDK replay proof")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    attempts = []

    def send(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.RemoteProtocolError("synthetic closed connection", request=request)
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "inclusionai/ling-3.1-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "synthetic"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            request=request,
        )

    async def request() -> None:
        from co_scientist.platform.llm.request.backend import LitellmBackend
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        monkeypatch.setattr(
            AsyncHTTPHandler,
            "create_client",
            lambda *args, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(send)),
        )
        backend = quality_benchmark.BoundedBackend(LitellmBackend(), 2)
        with pytest.raises(LLMTimeoutError):
            await backend.complete(
                model="openrouter/inclusionai/ling-3.1-flash",
                api_key="synthetic-unusable-test-key",
                messages=[{"role": "user", "content": "synthetic"}],
            )
        assert backend.calls == len(attempts) == 1

    with count_http_attempts():
        asyncio.run(request())


class FakeBackend:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.requests: list[dict[str, Any]] = []

    def supports_json_schema(self, model_name: str) -> bool:
        return model_name == "fake"

    async def complete(self, **completion_args: Any) -> Any:
        self.requests.append(completion_args)
        if self.fail:
            raise RuntimeError("synthetic refused request")
        return {"model": completion_args["model"]}


def test_concurrent_dispatches_stop_at_the_physical_allowance() -> None:
    backend = BoundedBackend(FakeBackend(), 5)

    def request(_: int) -> bool:
        try:
            asyncio.run(backend.complete(model="fake"))
            return True
        except LLMCallBudgetExceededError:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(request, range(40)))
    assert sum(results) == backend.calls == 5
    assert backend.supports_json_schema("fake")
    assert not backend.supports_json_schema("other")


def test_failed_provider_attempts_still_consume_the_allowance() -> None:
    backend = BoundedBackend(FakeBackend(fail=True), 1)
    with pytest.raises(RuntimeError, match="synthetic"):
        asyncio.run(backend.complete(model="fake"))
    with pytest.raises(LLMCallBudgetExceededError):
        asyncio.run(backend.complete(model="fake"))
    assert backend.calls == 1


def test_sdk_retry_options_are_cleared_on_backend_delegation() -> None:
    delegate = FakeBackend()
    backend = BoundedBackend(delegate, 1)
    asyncio.run(backend.complete(model="fake", num_retries=9, max_retries=9))
    assert delegate.requests == [{"model": "fake", "num_retries": 0, "max_retries": 0}]


def test_invalid_allowance_and_existing_database_refuse_before_model_import(
    tmp_path: Path,
) -> None:
    for ceiling in (0, 451):
        with pytest.raises(ValueError, match="between 1 and 450"):
            collect("cell-biology", tmp_path, live=False, ceiling=ceiling, tier="express")
    database = tmp_path / "run.db"
    database.write_bytes(b"do not overwrite")
    with pytest.raises(ValueError, match="new output directory"):
        collect("cell-biology", tmp_path, live=False, ceiling=150, tier="express")
    assert database.read_bytes() == b"do not overwrite"


def test_offline_collection_preserves_refusal_database_receipt_and_table(
    tmp_path: Path,
) -> None:
    import json
    import os
    import subprocess
    import sys

    code = """
import runpy, socket, sys
def deny(*args, **kwargs):
    raise AssertionError("network access during offline collection")
socket.socket.connect = deny
socket.create_connection = deny
sys.argv = ["quality_benchmark", "--goal-id", "urban-hydrology",
            "--output", sys.argv[1], "--max-calls", "3"]
runpy.run_module("evaluations.quality_benchmark", run_name="__main__")
"""
    env = {
        **os.environ,
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "run")],
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    output = tmp_path / "run"
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["physical_requests_attempted"] == receipt["metrics"]["physical_calls"] == 3
    assert receipt["metrics"]["completed"] is False
    assert receipt["metrics"]["total_tokens"] is None
    assert receipt["offline_disclaimer"]
    claim_report = json.loads((output / "claim-support-offline.json").read_text())
    assert claim_report["offline_disclaimer"]
    assert claim_report["provenance"]["source"]["git_commit"] == receipt["source_commit"]
    assert (output / "snapshot.db").stat().st_size > 0
    assert "urban-hydrology" in (output / "baseline.md").read_text()
