from __future__ import annotations

from typing import cast

import pytest
from co_scientist.api import runs as routes
from co_scientist.api.runs import stream_admission
from fastapi import HTTPException, Request
from starlette.responses import StreamingResponse
from starlette.types import Message

from tests._store_helpers import seed_run


async def _receive() -> Message:
    return {"type": "http.disconnect"}


async def _send(message: Message) -> None:
    pass


def _request(owner: str, peer: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/runs/load/events",
            "headers": [(b"x-client-id", owner.encode()), (b"x-forwarded-for", b"203.0.113.200")],
            "client": (peer, 1234),
            "asgi": {"spec_version": "2.4"},
        },
        receive=_receive,
    )


@pytest.mark.parametrize("pages,admitted", [(200, 200), (257, 256)])
async def test_get_streams_use_distinct_connecting_peers_without_write_context(
    pages: int, admitted: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream_admission, "_MAX_CONNECTIONS", 256)
    held: list[tuple[StreamingResponse, Request]] = []
    try:
        for index in range(pages):
            owner = f"viewer-{index}"
            run = seed_run("load stream", client_id=owner)
            request = _request(owner, f"198.18.{index // 250}.{index % 250 + 1}")
            if index == admitted:
                with pytest.raises(HTTPException) as denied:
                    await routes.stream_events(run.id, request, 0, True)
                assert denied.value.status_code == 429
                assert denied.value.headers == {"Retry-After": "30"}
            else:
                response = await routes.stream_events(run.id, request, 0, True)
                held.append((cast(StreamingResponse, response), request))
        assert len(held) == admitted
    finally:
        for response, request in held:
            await response(request.scope, _receive, _send)


async def test_forwarded_headers_do_not_evade_a_shared_connecting_peer_limit() -> None:
    held: list[tuple[StreamingResponse, Request]] = []
    try:
        for index in range(9):
            owner = f"nat-viewer-{index}"
            run = seed_run("shared network", client_id=owner)
            request = _request(owner, "198.51.100.1")
            request.scope["headers"][-1] = (b"x-forwarded-for", f"203.0.113.{index}".encode())
            if index == 8:
                with pytest.raises(HTTPException) as denied:
                    await routes.stream_events(run.id, request, 0, True)
                assert denied.value.status_code == 429
                assert denied.value.headers == {"Retry-After": "30"}
                assert "try again" in str(denied.value.detail).lower()
            else:
                response = await routes.stream_events(run.id, request, 0, True)
                held.append((cast(StreamingResponse, response), request))
    finally:
        for response, request in held:
            await response(request.scope, _receive, _send)
