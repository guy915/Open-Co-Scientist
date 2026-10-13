from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from co_scientist.api.deployment_control_api import DeploymentAdmissionMiddleware
from co_scientist.core import inflight
from co_scientist.core.async_bridge import off_loop
from co_scientist.platform.llm import complete_request, offline_guard
from co_scientist.platform.llm.admission.service import scoped_client
from co_scientist.platform.llm.request.cache import HAIKU
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from tests._client import make_client, make_operator_client
from tests._llm_fake_backend import completion_response, install_completion_backend


def test_deployment_barrier_stays_closed_until_the_next_process() -> None:
    client = make_client()
    operator = make_operator_client()
    endpoint = "/api/deployment/quiesce"

    assert client.post(endpoint).status_code == 403
    assert client.get(endpoint).status_code == 403
    assert operator.get(endpoint).json()["quiescing"] is False
    response = operator.post(endpoint)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["ready"] is True
    assert operator.post(endpoint).json() == response.json()
    assert client.post("/api/interviews", json={}).status_code == 503
    assert (
        operator.put(
            "/api/launch-control", json={"paused": True, "expected_revision": 0}
        ).status_code
        == 503
    )
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("cancel", [False, True])
async def test_quiesce_waits_for_an_accepted_token_count_before_allowing_redeploy(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch, reachable_provider: None, cancel: bool
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    for name, value in {
        "ANTHROPIC_API_KEY": "synthetic-key",
        "ANTHROPIC_MONTHLY_CREDIT_USD": "100",
        "ANTHROPIC_BILLING_RESET_DAY": "7",
        "LLM_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)

    async def send(
        _client: httpx.AsyncClient, request: httpx.Request, **_kwargs: Any
    ) -> httpx.Response:
        assert request.url.path.endswith("count_tokens")
        entered.set()
        await release.wait()
        return httpx.Response(200, request=request, json={"input_tokens": 100})

    async def provider(**_kwargs: Any) -> Any:
        response = completion_response("accepted")
        response.usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=2,
            prompt_tokens_details=None,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    install_completion_backend(monkeypatch, provider)
    with scoped_client("preflight-owner", db_path=isolated_db):
        pending = asyncio.create_task(
            complete_request(
                {
                    "model": HAIKU,
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": "question"}],
                },
                HAIKU,
                byok=False,
                timeout_seconds=5,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        operator = make_operator_client()
        try:
            assert operator.post("/api/deployment/quiesce").json()["ready"] is False
        finally:
            if cancel:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                release.set()
                await asyncio.wait_for(pending, timeout=5)
        assert operator.get("/api/deployment/quiesce").json()["ready"] is True


async def test_an_unknown_provider_outcome_does_not_report_safe_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider(**_kwargs: Any) -> Any:
        raise httpx.RemoteProtocolError("synthetic lost response")

    monkeypatch.setattr(offline_guard, "require_remote_chat", lambda _: None)
    install_completion_backend(monkeypatch, provider)
    with pytest.raises(httpx.RemoteProtocolError):
        await complete_request(
            {"model": "gpt-4o-mini"}, "gpt-4o-mini", byok=False, timeout_seconds=5
        )
    assert make_operator_client().post("/api/deployment/quiesce").json()["ready"] is False


async def test_a_copied_context_cannot_dispatch_after_its_operation_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    begin = asyncio.Event()
    requests: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return completion_response("unexpected")

    async def late() -> None:
        await begin.wait()
        await complete_request(
            {"model": "gpt-4o-mini"}, "gpt-4o-mini", byok=False, timeout_seconds=5
        )

    monkeypatch.setattr(offline_guard, "require_remote_chat", lambda _: None)
    install_completion_backend(monkeypatch, provider)
    with inflight.operation_scope() as admitted:
        assert admitted
        pending = asyncio.create_task(late())
    operator = make_operator_client()
    assert operator.post("/api/deployment/quiesce").json()["ready"] is True
    begin.set()
    await asyncio.sleep(0.01)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert requests == []
    assert operator.get("/api/deployment/quiesce").json()["ready"] is True


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_request_stays_in_the_barrier_until_its_handler_finishes(
    cancellations: int,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    saved = threading.Event()
    app = FastAPI()
    app.add_middleware(DeploymentAdmissionMiddleware)

    @app.post("/write")
    @off_loop
    def write() -> dict[str, bool]:
        entered.set()
        assert release.wait(timeout=5)
        saved.set()
        return {"saved": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        pending = asyncio.create_task(client.post("/write"))
        assert await asyncio.to_thread(entered.wait, 5)
        operator = make_operator_client()
        assert operator.post("/api/deployment/quiesce").json()["ready"] is False
        for _ in range(cancellations):
            pending.cancel()
            await asyncio.sleep(0.01)
        try:
            assert operator.get("/api/deployment/quiesce").json()["ready"] is False
            assert not saved.is_set()
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            assert await asyncio.to_thread(saved.wait, 5)
        assert operator.get("/api/deployment/quiesce").json()["ready"] is True


async def test_accepted_stream_finishes_and_persists_before_the_barrier_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamed = asyncio.Event()
    finish = asyncio.Event()
    persisted: list[str] = []
    model = "gpt-4o-mini"

    async def chunks() -> AsyncIterator[Any]:
        yield SimpleNamespace(choices=[], model=model, usage=None)
        streamed.set()
        await finish.wait()
        yield SimpleNamespace(choices=[], model=model, usage=None)

    async def provider(**_kwargs: Any) -> Any:
        return chunks()

    monkeypatch.setattr(offline_guard, "require_remote_chat", lambda _: None)
    install_completion_backend(monkeypatch, provider)
    app = FastAPI()
    app.add_middleware(DeploymentAdmissionMiddleware)

    @app.post("/reply")
    async def reply() -> StreamingResponse:
        async def body() -> AsyncIterator[str]:
            response = await complete_request(
                {"model": model, "stream": True}, model, byok=False, timeout_seconds=5
            )
            async for _ in response:
                yield "data: response\n\n"
            persisted.append("saved response")

        return StreamingResponse(body(), media_type="text/event-stream")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        pending = asyncio.create_task(client.post("/reply"))
        await asyncio.wait_for(streamed.wait(), timeout=5)
        operator = make_operator_client()
        try:
            assert operator.post("/api/deployment/quiesce").json()["ready"] is False
            assert persisted == []
        finally:
            finish.set()
        response = await asyncio.wait_for(pending, timeout=5)
        assert response.status_code == 200
        assert persisted == ["saved response"]
        assert operator.get("/api/deployment/quiesce").json()["ready"] is True
