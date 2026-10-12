from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from co_scientist.api.interviews import turns
from co_scientist.api.launch_admission import chat_response
from co_scientist.core import byok_scope
from co_scientist.core.sse import sse_frame
from co_scientist.domains.chat.repository import interviews as store
from co_scientist.platform.db.launch_control import LaunchControl

logger = logging.getLogger(__name__)

# One queue preserves reasoning and prose order across independent coroutines.
_Fragment = tuple[str, str]


async def _start_stream_advance(
    interview_id: str,
) -> tuple[asyncio.Queue[_Fragment | None], asyncio.Task[dict[str, Any]]]:
    queue: asyncio.Queue[_Fragment | None] = asyncio.Queue()

    async def _on_reasoning(fragment: str) -> None:
        await queue.put(("reasoning", fragment))

    async def _on_prose(fragment: str) -> None:
        await queue.put(("chunk", fragment))

    task = asyncio.create_task(turns.advance_turn(interview_id, _on_reasoning, _on_prose))
    task.add_done_callback(lambda _: queue.put_nowait(None))
    return queue, task


async def _advance_stream(
    interview_id: str,
    byok: byok_scope.ByokCredential | None = None,
) -> AsyncGenerator[str, None]:
    """One ordered queue carries reasoning and prose; disconnect
    cancellation stops unwatched provider work.
    """
    if store.get_interview(interview_id) is None:
        yield sse_frame({"type": "error", "detail": "interview not found"})
        return
    with byok_scope.scoped_byok(byok):
        queue, task = await _start_stream_advance(interview_id)
        try:
            while (fragment := await queue.get()) is not None:
                kind, content = fragment
                yield sse_frame({"type": kind, "content": content})

            error_frame, updated = await _resolve_advance_task(task, interview_id)
            if error_frame is not None:
                yield error_frame
                return
            yield sse_frame({"type": "interview", "interview": updated})
        finally:
            await _cancel_pending(task)


async def _cancel_pending(task: asyncio.Task[dict[str, Any]]) -> None:
    """Await cancellation so disconnected streams leave no pending task
    behind.
    """
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _resolve_advance_task(
    task: asyncio.Task[dict[str, Any]], interview_id: str
) -> tuple[str | None, dict[str, Any] | None]:
    try:
        return None, await task
    except HTTPException as exc:
        return sse_frame({"type": "error", "detail": str(exc.detail)}), None
    except Exception:
        logger.exception("Interview turn failed for %s", interview_id)
        return (
            sse_frame({"type": "error", "detail": "The interview Agent failed."}),
            None,
        )


def _interview_stream(
    interview_id: str,
    byok: byok_scope.ByokCredential | None = None,
    *,
    control: LaunchControl | None = None,
) -> StreamingResponse:
    return chat_response(
        StreamingResponse(
            _advance_stream(interview_id, byok),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        ),
        control=control,
    )
