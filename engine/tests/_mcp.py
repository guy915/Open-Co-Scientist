from __future__ import annotations

import types
from typing import Any, ClassVar, cast

import pytest
from langchain_core.tools import StructuredTool

from co_scientist.platform.llm.offline import llm as offline_llm
from tests._llm_fake import restore_backend_at_teardown


def string_tool(name: str, result: Any) -> StructuredTool:

    async def _impl(**_: Any) -> Any:
        return result

    return StructuredTool.from_function(
        coroutine=_impl,
        name=name,
        description=f"fake tool {name}",
    )


class FakeToolResultsClient:
    def __init__(
        self,
        results: dict[str, Any] | None = None,
        available_tools: set[str] | None = None,
        error_tools: set[str] | None = None,
    ) -> None:
        self._results = results or {}
        self._available_tools = available_tools
        self._error_tools = error_tools or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        self.calls.append((tool_name, kwargs))
        if tool_name in self._error_tools:
            raise RuntimeError(f"tool failed: {tool_name}")
        return self._results.get(tool_name)

    def has_tool(self, tool_name: str) -> bool:
        if self._available_tools is None:
            return True
        return tool_name in self._available_tools


def make_tool_results_client(
    results: dict[str, Any] | None = None,
    available_tools: set[str] | None = None,
    error_tools: set[str] | None = None,
) -> Any:
    return cast(
        Any,
        FakeToolResultsClient(results, available_tools, error_tools),
    )


def stub_mcp_availability(monkeypatch: pytest.MonkeyPatch, *, available: bool) -> None:
    from co_scientist.platform.retrieval import mcp_client

    async def fake(**_: Any) -> bool:
        return available

    monkeypatch.setattr(mcp_client, "check_mcp_available", fake)
    monkeypatch.setattr(mcp_client, "check_literature_source_available", fake)


class ToolLookupRegistry:
    def __init__(self, tools: dict[str, Any]) -> None:
        self._tools = tools

    def get_tool(self, tool_id: str) -> Any:
        return self._tools.get(tool_id)


def make_tool_lookup_registry(tools: dict[str, Any]) -> Any:
    return cast(Any, ToolLookupRegistry(tools))


class FakeMultiServerMCPClient:
    instances_created = 0
    tools: ClassVar[list[StructuredTool]] = []
    error: Exception | None = None

    def __init__(self, connections: Any, *, handle_tool_errors: bool = True) -> None:
        type(self).instances_created += 1
        self.connections = connections
        self.handle_tool_errors = handle_tool_errors

    async def get_tools(self) -> list[StructuredTool]:
        err = type(self).error
        if err is not None:
            raise err
        return list(type(self).tools)


def make_tool_call(name: str, arguments: str, call_id: str = "call-1") -> Any:
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


def isolate_offline_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the process-wide backend and idempotency flag so later tests
    start fresh."""
    restore_backend_at_teardown(monkeypatch)
    monkeypatch.setattr(offline_llm, "_installed", False)
