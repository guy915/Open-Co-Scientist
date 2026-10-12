from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from co_scientist.api import diagnostics
from co_scientist.api.diagnostics import PROBE_DOWN, PROBE_UP, ProbeResult
from co_scientist.core.config import settings


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _triple(state: str) -> tuple[ProbeResult, ProbeResult, ProbeResult]:
    result = ProbeResult(available=state == PROBE_UP, state=state)
    return result, result, result


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


async def test_concurrent_cold_callers_share_one_probe(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    calls = 0
    release = asyncio.Event()

    async def probe() -> tuple[ProbeResult, ProbeResult, ProbeResult]:
        nonlocal calls
        calls += 1
        await release.wait()
        return _triple(PROBE_UP)

    monkeypatch.setattr(diagnostics, "_probe_literature_stack", probe)

    waiters = [asyncio.create_task(diagnostics.probe_literature_stack_cached()) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*waiters)

    assert calls == 1
    assert all(result[0].state == PROBE_UP for result in results)


async def test_expired_results_are_served_while_one_refresh_runs(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    states = iter([PROBE_UP, PROBE_DOWN])
    calls = 0

    async def probe() -> tuple[ProbeResult, ProbeResult, ProbeResult]:
        nonlocal calls
        calls += 1
        return _triple(next(states))

    monkeypatch.setattr(diagnostics, "_probe_literature_stack", probe)
    await diagnostics.probe_literature_stack_cached()
    clock.now += settings.status_probe_cache_ttl_seconds + 1

    stale = [await diagnostics.probe_literature_stack_cached() for _ in range(3)]
    await asyncio.sleep(0)
    fresh = await diagnostics.probe_literature_stack_cached()

    assert [result[0].state for result in stale] == [PROBE_UP] * 3
    assert fresh[0].state == PROBE_DOWN
    assert calls == 2


async def test_results_older_than_the_stale_window_wait_for_a_fresh_probe(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    states = iter([PROBE_UP, PROBE_DOWN])

    async def probe() -> tuple[ProbeResult, ProbeResult, ProbeResult]:
        return _triple(next(states))

    monkeypatch.setattr(diagnostics, "_probe_literature_stack", probe)
    await diagnostics.probe_literature_stack_cached()
    clock.now += settings.status_probe_cache_ttl_seconds + diagnostics._STALE_SERVE_SECONDS + 1

    result = await diagnostics.probe_literature_stack_cached()

    assert result[0].state == PROBE_DOWN
