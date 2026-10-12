from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from co_scientist.api import runs as routes
from co_scientist.api.runs import events, stream_admission
from co_scientist.platform.db import runs
from fastapi import HTTPException, Request
from starlette.responses import StreamingResponse
from starlette.types import Message

from tests._store_helpers import seed_run


async def _disconnected() -> dict[str, Any]:
    return {"type": "http.disconnect"}


def _request(owner: str = "synthetic-owner", peer: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/runs/synthetic/events",
            "headers": [(b"x-client-id", owner.encode())],
            "client": (peer, 1234),
            "asgi": {"spec_version": "2.4"},
        },
        receive=_disconnected,
    )


async def _finish(response: StreamingResponse, request: Request) -> None:
    async def send(message: Message) -> None:
        pass

    await response(request.scope, _disconnected, send)


async def test_repeated_run_event_requests_have_an_owner_connection_ceiling() -> None:
    run = seed_run("synthetic stream", client_id="synthetic-owner")
    request = _request()
    held: list[StreamingResponse] = []
    try:
        for _ in range(4):
            held.append(
                cast(StreamingResponse, await routes.stream_events(run.id, request, 0, True))
            )
        with pytest.raises(HTTPException) as denied:
            await routes.stream_events(run.id, request, 0, True)
        assert denied.value.status_code == 429
    finally:
        for response in held:
            await _finish(response, request)


async def test_abandoned_draft_tail_closes_without_a_terminal_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = seed_run("abandoned draft", client_id="synthetic-owner")
    monkeypatch.setattr(events, "_TICK_SECONDS", 0.001)
    monkeypatch.setattr(events, "_DRAFT_IDLE_SECONDS", 0.002, raising=False)

    async def connected() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = _request()
    request._receive = connected

    async def consume() -> list[str]:
        return [frame async for frame in events._event_stream(run.id, request, 0, run)]

    frames = await asyncio.wait_for(consume(), 0.25)
    assert not any('"_terminal"' in frame for frame in frames)
    current = runs.get_run(run.id)
    assert current is not None and current.status == "draft"


@pytest.mark.parametrize("boundary", ["peer", "global"])
async def test_identity_rotation_cannot_exhaust_event_subscriptions(
    boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream_admission, "_MAX_CONNECTIONS", 64)
    held: list[tuple[StreamingResponse, Request]] = []
    owners = 3 if boundary == "peer" else 17
    limit = 8 if boundary == "peer" else 64
    try:
        for index in range(owners):
            owner = f"synthetic-owner-{index}"
            peer = "same-peer" if boundary == "peer" else f"peer-{index // 2}"
            run = seed_run("synthetic stream", client_id=owner)
            request = _request(owner, peer)
            for _ in range(4):
                if len(held) == limit:
                    with pytest.raises(HTTPException) as denied:
                        await routes.stream_events(run.id, request, 0, True)
                    assert denied.value.status_code == 429
                    return
                response = await routes.stream_events(run.id, request, 0, True)
                held.append((cast(StreamingResponse, response), request))
        pytest.fail("aggregate admission was not reached")
    finally:
        for response, request in held:
            await _finish(response, request)


@pytest.mark.parametrize("cancel", [False, True])
async def test_response_failure_releases_admission_before_any_body_iteration(cancel: bool) -> None:
    run = seed_run("synthetic stream", client_id="synthetic-owner")
    request = _request()
    held = [
        cast(StreamingResponse, await routes.stream_events(run.id, request, 0, True))
        for _ in range(4)
    ]
    started = asyncio.Event()

    async def send(message: Message) -> None:
        started.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("synthetic send failure")

    try:
        task = asyncio.create_task(held[0](request.scope, _disconnected, send))
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError, match="synthetic send failure"):
                await task
        replacement = cast(StreamingResponse, await routes.stream_events(run.id, request, 0, True))
        await _finish(replacement, request)
        snapshot = await routes.stream_events(run.id, request, 0, False)
        assert snapshot.status_code == 200
    finally:
        for response in held[1:]:
            await _finish(response, request)
