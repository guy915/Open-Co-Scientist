from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncIterator

from fastapi import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from co_scientist.core.sse import sse_frame
from co_scientist.platform.db import current_time
from co_scientist.platform.db.launch_control import (
    LaunchControl,
    LaunchPausedError,
    read_control,
    require_unpaused,
)


def paused_response(exc: LaunchPausedError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if exc.control.resumes_at is not None and exc.control.resumes_at > current_time():
        headers["Retry-After"] = str(max(1, int(exc.control.resumes_at - current_time())))
    return JSONResponse(
        {"detail": str(exc), "code": "launch_paused", "resumes_at": exc.control.resumes_at},
        status_code=503,
        headers=headers,
    )


async def paused_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, LaunchPausedError)
    return paused_response(exc)


class LaunchAdmissionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in {"POST", "PUT", "PATCH"}:
            path = scope.get("path", "").rstrip("/")
            if (
                path == "/api/runs"
                or path == "/api/interviews"
                or path.startswith("/api/interviews/")
                or re.fullmatch(
                    r"/api/runs/[^/]+/(?:start|resume|example-chat|messages(?:/.*)?|attachments(?:/upload)?)",
                    path,
                )
            ):
                try:
                    await asyncio.to_thread(require_unpaused)
                except LaunchPausedError as exc:
                    await paused_response(exc)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


async def _drainable_stream(
    source: AsyncIterator[str | bytes | memoryview],
    generation: int,
) -> AsyncIterator[str | bytes | memoryview]:
    queue: asyncio.Queue[
        tuple[str | bytes | memoryview | None, Exception | None, asyncio.Future[None] | None]
    ] = asyncio.Queue(maxsize=1)

    async def produce() -> None:
        try:
            async for chunk in source:
                ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                await queue.put((chunk, None, ack))
                await ack
        except Exception as exc:
            await queue.put((None, exc, None))
        else:
            await queue.put((None, None, None))
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()

    # Credential ContextVars must enter, yield and exit on one producer task.
    # Acknowledgement keeps provider read-ahead bounded to one fragment.
    producer = asyncio.create_task(produce())
    due = 0.0
    try:
        while True:
            if time.monotonic() >= due:
                control = await asyncio.to_thread(read_control)
                due = time.monotonic() + 1.0
                if control.drain_generation != generation:
                    producer.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await producer
                    yield sse_frame(
                        {"type": "error", "detail": control.message, "message": control.message}
                    )
                    return
            try:
                chunk, error, ack = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if error is not None:
                raise error
            if chunk is None:
                return
            try:
                yield chunk
            finally:
                if ack is not None and not ack.done():
                    ack.set_result(None)
    finally:
        if not producer.done():
            producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer


def chat_response(
    response: StreamingResponse, *, control: LaunchControl | None = None
) -> StreamingResponse:
    # Admission ends before network dispatch; a later ordinary pause must not
    # interrupt this admitted reply. A drain generation survives rapid resume.
    control = control if control is not None else require_unpaused()
    response.body_iterator = _drainable_stream(
        aiter(response.body_iterator), control.drain_generation
    )
    return response
