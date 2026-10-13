from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

from co_scientist.core.exceptions import LLMTimeoutError
from co_scientist.platform.db import connect
from co_scientist.platform.llm.admission.call_budget import scoped_llm_call_budget
from co_scientist.platform.llm.request.backend import LitellmBackend, using_backend
from co_scientist.platform.llm.request.transport import complete_request

MODEL = "openrouter/inclusionai/ling-3.1-flash"


@pytest.fixture(autouse=True)
async def _drain_sdk_logging() -> AsyncIterator[None]:
    # Bind the SDK's global worker to this case's loop first: flushing a queue
    # left by an earlier, closed loop with an unfinished item waits forever.
    GLOBAL_LOGGING_WORKER.start()
    yield
    # The real SDK's queued callbacks must finish before pytest closes this
    # case's event loop and creates the next one.
    await GLOBAL_LOGGING_WORKER.flush()
    await GLOBAL_LOGGING_WORKER.stop()


@pytest.mark.parametrize("byok", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("disconnect", [None, httpx.RemoteProtocolError, httpx.ConnectError])
async def test_real_sdk_sends_once_and_settles_only_known_usage(
    monkeypatch: pytest.MonkeyPatch,
    byok: bool,
    stream: bool,
    disconnect: type[httpx.RequestError] | None,
) -> None:
    sent = []

    async def send(
        _client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        assert request.method == "POST"
        sent.append(request)
        if len(sent) == 1 and disconnect is not None:
            raise disconnect("synthetic disconnect", request=request)
        response = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk" if stream else "chat.completion",
            "created": 1,
            "model": MODEL.removeprefix("openrouter/"),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "answer"},
                    "delta": {"content": "answer"},
                }
            ],
            "usage": {"prompt_tokens": 90, "completion_tokens": 30, "total_tokens": 120},
        }
        if stream:
            final = {**response, "choices": []}
            response.pop("usage")
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                text=(
                    "data: "
                    + json.dumps(response)
                    + "\n\ndata: "
                    + json.dumps(final)
                    + "\n\ndata: [DONE]\n\n"
                ),
            )
        return httpx.Response(200, request=request, json=response)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with using_backend(LitellmBackend()), scoped_llm_call_budget("sdk-post", 3):
        request = complete_request(
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": "answer"}],
                "max_tokens": 1000,
                "api_key": "fake",
                "stream": stream,
                **({"stream_options": {"include_usage": True}} if stream else {}),
            },
            MODEL,
            byok=byok,
            timeout_seconds=5,
        )
        if disconnect is not None:
            with pytest.raises(LLMTimeoutError) as caught:
                await request
            assert caught.value.zero_cost_admitted is (not byok)
        else:
            response = await request
            if stream:
                async for _chunk in response:
                    pass
    assert len(sent) == 1
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT calls FROM run_call_admissions WHERE run_id='sdk-post'"
            ).fetchone()[0]
            == 1
        )
        rows = conn.execute("SELECT calls FROM provider_admissions WHERE scope='global'").fetchall()
        assert [row[0] for row in rows] == ([] if byok else [1])
        if not byok:
            used = conn.execute("SELECT used_tokens FROM provider_token_reservations").fetchone()[0]
            assert used == (None if disconnect is not None else 120)
