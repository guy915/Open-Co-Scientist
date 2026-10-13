from __future__ import annotations

from typing import Any, cast

import pytest

from co_scientist.platform.retrieval.evidence import search_query
from co_scientist.platform.retrieval.mcp_client import MCPToolClient, UnknownToolError


class _Client:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def call_tool(self, tool_name: str, **kwargs: Any) -> str:
        self.calls += 1
        raise self.error


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    def record(attempt: int) -> float:
        slept.append(attempt)
        return 0.0

    monkeypatch.setattr(search_query, "_search_retry_delay", record)
    return slept


async def test_an_unregistered_tool_fails_once_without_waiting(sleeps: list[float]) -> None:
    client = _Client(UnknownToolError("tool 'search_web' not found"))

    with pytest.raises(UnknownToolError):
        await search_query.call_search_tool(cast(MCPToolClient, client), "search_web", {})

    assert client.calls == 1
    assert sleeps == []


async def test_a_transient_failure_is_still_retried(sleeps: list[float]) -> None:
    client = _Client(ConnectionError("session reset"))

    with pytest.raises(ConnectionError):
        await search_query.call_search_tool(cast(MCPToolClient, client), "search_pubmed", {})

    assert client.calls == search_query._SEARCH_ATTEMPTS
    assert len(sleeps) == search_query._SEARCH_ATTEMPTS - 1
