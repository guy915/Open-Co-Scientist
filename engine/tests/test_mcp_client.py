from __future__ import annotations

import asyncio
import json
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
