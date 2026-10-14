from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

import co_scientist.platform.retrieval.mcp_client as mcp_client_module
from co_scientist.core.exceptions import MCPToolTimeoutError
from co_scientist.platform.retrieval.mcp_client import (
    MCP_AUTH_HEADER,
    MCP_SHARED_SECRET_ENV,
    MCPToolClient,
)
from tests._mcp import (
    FakeMultiServerMCPClient,
    make_tool_call,
    string_tool,
)

# MCP calls need their own timeout; the LLM timeout cannot cover a hung SSE tool
# call.


class _HungTool:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def ainvoke(self, _args: Any) -> str:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return "never"


async def test_a_hung_tool_is_cancelled_and_reported_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raising under gather would cancel sibling tools; return this tool's
    timeout to the model."""
    monkeypatch.setenv(mcp_client_module.MCP_TOOL_TIMEOUT_ENV, "0.05")
    hung = _HungTool()
    client = MCPToolClient()
    client._tools_dict = {"search_pubmed": hung}

    with pytest.raises(MCPToolTimeoutError, match="search_pubmed"):
        await client.call_tool("search_pubmed", query="anything")
    await asyncio.wait_for(hung.cancelled.wait(), timeout=1.0)

    message = await client.execute_tool_call(
        make_tool_call("search_pubmed", json.dumps({"query": "anything"}))
    )
    assert message["role"] == "tool"
    assert message["name"] == "search_pubmed"
    assert "did not respond" in message["content"]


@pytest.mark.parametrize(
    ("secret", "explicit", "expected"),
    [
        (None, None, None),
        ("", None, None),
        ("topsecret", None, {MCP_AUTH_HEADER: "topsecret"}),
        (
            "topsecret",
            {"X-Other": "kept"},
            {"X-Other": "kept", MCP_AUTH_HEADER: "topsecret"},
        ),
    ],
    ids=["unset", "empty-is-unset", "attached", "merged-with-existing"],
)
async def test_the_shared_secret_reaches_the_connection_as_a_header(
    monkeypatch: pytest.MonkeyPatch,
    _patch_mcp_seam: type[FakeMultiServerMCPClient],
    secret: str | None,
    explicit: dict[str, str] | None,
    expected: dict[str, str] | None,
) -> None:
    _patch_mcp_seam.tools = [string_tool("t1", "ok")]
    if secret is None:
        monkeypatch.delenv(MCP_SHARED_SECRET_ENV, raising=False)
    else:
        monkeypatch.setenv(MCP_SHARED_SECRET_ENV, secret)
    config: dict[str, Any] = {
        "transport": "streamable_http",
        "url": "http://s1.test/mcp",
    }
    if explicit is not None:
        config["headers"] = explicit
    client = MCPToolClient(server_configs={"s1": config})

    await client.initialize()

    assert client._client is not None
    assert client._client.connections["s1"].get("headers") == expected
    assert config.get("headers") == explicit


async def test_a_tool_execution_error_raises_instead_of_reading_as_a_result(
    _patch_mcp_seam: type[FakeMultiServerMCPClient],
) -> None:
    _patch_mcp_seam.tools = [string_tool("t1", "ok")]
    client = MCPToolClient(server_configs={"s1": {"transport": "streamable_http", "url": "x"}})

    await client.initialize()

    assert isinstance(client._client, FakeMultiServerMCPClient)
    assert client._client.handle_tool_errors is False


async def test_concurrent_clients_wait_for_available_retrieval_capacity() -> None:
    active = 0
    peak = 0
    full = asyncio.Event()
    release = asyncio.Event()

    class CapacityTool:
        async def ainvoke(self, _args: Any) -> str:
            nonlocal active, peak
            if active == 8:
                return "Retrieval capacity is busy"
            active += 1
            peak = max(peak, active)
            if active == 8:
                full.set()
            try:
                await release.wait()
                return "ok"
            finally:
                active -= 1

    tool = CapacityTool()
    clients = [MCPToolClient(), MCPToolClient()]
    for client in clients:
        client._tools_dict = {"search_pubmed": tool}
    calls = [
        asyncio.create_task(clients[index % 2].call_tool("search_pubmed", query="anything"))
        for index in range(12)
    ]
    try:
        await asyncio.wait_for(full.wait(), timeout=1)
    finally:
        release.set()
    results = await asyncio.gather(*calls)
    assert results == ["ok"] * 12
    assert peak == 8


async def test_retrieval_capacity_is_shared_across_cohort_event_loops() -> None:
    active = 0
    full = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    class CapacityTool:
        async def ainvoke(self, _args: Any) -> str:
            nonlocal active
            with lock:
                if active == 8:
                    return "Retrieval capacity is busy"
                active += 1
                if active == 8:
                    full.set()
            try:
                while not release.is_set():
                    await asyncio.sleep(0.001)
                return "ok"
            finally:
                with lock:
                    active -= 1

    tool = CapacityTool()

    def run_cohort() -> list[Any]:
        async def run() -> list[Any]:
            client = MCPToolClient()
            client._tools_dict = {"search_pubmed": tool}
            return await asyncio.gather(
                *(client.call_tool("search_pubmed") for _ in range(3)),
                *(
                    client.execute_tool_call(make_tool_call("search_pubmed", "{}"))
                    for _ in range(3)
                ),
            )

        return asyncio.run(run())

    cohorts = [asyncio.create_task(asyncio.to_thread(run_cohort)) for _ in range(2)]
    try:
        assert await asyncio.to_thread(full.wait, 1)
    finally:
        release.set()
    for results in await asyncio.gather(*cohorts):
        assert results[:3] == ["ok"] * 3
        assert [result["content"] for result in results[3:]] == ["ok"] * 3


async def test_cancelling_a_waiting_call_does_not_release_an_occupied_slot() -> None:
    active = 0
    full = asyncio.Event()
    release = asyncio.Event()

    class CapacityTool:
        async def ainvoke(self, _args: Any) -> str:
            nonlocal active
            active += 1
            if active == 8:
                full.set()
            try:
                await release.wait()
                return "ok"
            finally:
                active -= 1

    client = MCPToolClient()
    client._tools_dict = {"search_pubmed": CapacityTool()}
    held = [asyncio.create_task(client.call_tool("search_pubmed")) for _ in range(8)]
    await asyncio.wait_for(full.wait(), 1)
    waiting = asyncio.create_task(client.call_tool("search_pubmed"))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiting), 0.03)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert active == 8
    finally:
        release.set()
    assert await asyncio.gather(*held) == ["ok"] * 8
    assert await client.call_tool("search_pubmed") == "ok"
