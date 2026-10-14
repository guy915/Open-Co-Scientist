from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from co_scientist.core import inflight
from co_scientist.core.async_bridge import run_coroutine_sync, run_off_loop
from co_scientist.core.exceptions import LLMTimeoutError
from co_scientist.platform.llm import complete_request, offline_guard
from litellm.exceptions import APIConnectionError, RateLimitError

from tests._llm_fake_backend import install_completion_backend

_MODEL = "gpt-4o-mini"


def _response() -> Any:
    return SimpleNamespace(choices=[], model=_MODEL)


async def _call(task_id: str, **extra: Any) -> Any:
    with inflight.task_scope(task_id):
        return await complete_request(
            {"model": _MODEL, **extra}, _MODEL, byok=False, timeout_seconds=extra.pop("t", 5)
        )


def _install(monkeypatch: pytest.MonkeyPatch, provider: Callable[..., Any]) -> None:
    monkeypatch.setattr(offline_guard, "require_remote_chat", lambda _: None)
    install_completion_backend(monkeypatch, provider)


def _unanswered() -> frozenset[str]:
    snapshot = inflight.begin_shutdown()
    inflight.resume_dispatch()
    return snapshot


async def test_an_answered_call_clears_its_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    async def provider(**kwargs: Any) -> Any:
        return _response()

    _install(monkeypatch, provider)
    await _call("answered")

    assert "answered" not in _unanswered()


async def test_a_provider_status_error_is_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def provider(**kwargs: Any) -> Any:
        raise RateLimitError("slow down", llm_provider="openai", model=_MODEL)

    _install(monkeypatch, provider)
    with pytest.raises(RateLimitError):
        await _call("rate-limited")

    assert "rate-limited" not in _unanswered()


@pytest.mark.parametrize(
    "failure",
    [
        APIConnectionError("connection reset", llm_provider="openai", model=_MODEL),
        httpx.RemoteProtocolError("peer closed connection mid-response"),
        RuntimeError("unrecognized transport failure"),
    ],
)
async def test_a_call_without_a_provider_answer_keeps_its_mark(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    async def provider(**kwargs: Any) -> Any:
        raise failure

    _install(monkeypatch, provider)
    with pytest.raises(type(failure)):
        await _call("unknown-post")

    assert "unknown-post" in _unanswered()


async def test_a_timed_out_call_keeps_its_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    async def provider(**kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    _install(monkeypatch, provider)
    with pytest.raises(LLMTimeoutError):
        await _call("timed-out", t=0.01)

    assert "timed-out" in _unanswered()


async def _chunks() -> AsyncIterator[Any]:
    yield SimpleNamespace(choices=[], model=_MODEL, usage=None)
    yield SimpleNamespace(choices=[], model=_MODEL, usage=None)


async def test_a_finished_stream_clears_and_an_abandoned_stream_keeps_its_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider(**kwargs: Any) -> Any:
        return _chunks()

    _install(monkeypatch, provider)
    finished = await _call("finished-stream", stream=True)
    async for _ in finished:
        pass
    abandoned = await _call("abandoned-stream", stream=True)
    await abandoned.__anext__()
    await abandoned.aclose()

    unanswered = _unanswered()
    assert "finished-stream" not in unanswered
    assert "abandoned-stream" in unanswered


async def test_a_call_inside_an_off_loop_wave_is_attributed_to_its_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider(**kwargs: Any) -> Any:
        raise APIConnectionError("reset", llm_provider="openai", model=_MODEL)

    _install(monkeypatch, provider)

    async def dispatch() -> None:
        with pytest.raises(APIConnectionError):
            await complete_request({"model": _MODEL}, _MODEL, byok=False, timeout_seconds=5)

    with inflight.task_scope("wave-parent"):
        await run_off_loop(lambda: run_coroutine_sync(dispatch))

    assert "wave-parent" in _unanswered()


async def test_after_shutdown_a_dispatch_never_reaches_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return _response()

    _install(monkeypatch, provider)
    inflight.begin_shutdown()
    late = asyncio.create_task(_call("late-dispatch"))
    await asyncio.sleep(0.05)
    late.cancel()
    with pytest.raises(asyncio.CancelledError):
        await late

    assert requests == []
    assert "late-dispatch" not in inflight.begin_shutdown()
