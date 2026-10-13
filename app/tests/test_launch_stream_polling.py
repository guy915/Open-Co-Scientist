from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import ParamSpec, TypeVar, cast

import pytest
from co_scientist.api.runs import events
from co_scientist.core.sse import sse_frame
from fastapi import Request

P = ParamSpec("P")
T = TypeVar("T")


class Clock:
    now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    async def offload(self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        return function(*args, **kwargs)


async def test_quiet_stream_bounds_store_reads_without_delaying_events_or_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    polls: list[float] = []
    delivered: list[tuple[float, str]] = []
    emitted = False

    def poll(run_id: str, last_seq: int, check_status: bool) -> tuple[int, str | None, list[str]]:
        nonlocal emitted
        polls.append(clock.now)
        if clock.now >= 20 and check_status:
            return last_seq, "completed", []
        if clock.now >= 4 and not emitted:
            emitted = True
            return 1, None, [sse_frame({"seq": 1, "type": "progress", "payload": {}})]
        return last_seq, None, []

    async def connected() -> bool:
        return False

    monkeypatch.setattr(events, "_poll_tick", poll)
    monkeypatch.setattr(
        events,
        "asyncio",
        SimpleNamespace(get_running_loop=lambda: clock, sleep=clock.sleep, to_thread=clock.offload),
    )
    request = cast(Request, SimpleNamespace(is_disconnected=connected))
    async for frame in events._stream_live_tail("quiet-run", request, 0):
        delivered.append((clock.now, frame))

    assert len(polls) <= 16
    progress_time = next(at for at, frame in delivered if '"progress"' in frame)
    assert 4 <= progress_time <= 6
    assert next(at for at in polls if at > progress_time) - progress_time <= 0.5
    keepalive_time = next(at for at, frame in delivered if frame == ": keepalive\n\n")
    assert 15 <= keepalive_time - progress_time <= 17
    terminal_time = next(at for at, frame in delivered if '"_terminal"' in frame)
    assert 20 <= terminal_time <= 27
