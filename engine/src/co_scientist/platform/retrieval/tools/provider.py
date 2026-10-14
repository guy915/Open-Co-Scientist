import json
import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from co_scientist.core.exceptions import ConfigError
from co_scientist.platform.llm.tools.messages import (
    tool_error_message,
    tracked_executor,
)
from co_scientist.platform.retrieval.mcp_client import MCPToolClient

logger = logging.getLogger(__name__)


# Bound excerpts to recognize upstream HTML/throttling without logging entire
# bodies.
_PAYLOAD_EXCERPT_CHARS = 200


def parse_mcp_result(result: Any) -> Any:
    """Accept raw JSON text and already-decoded client payloads."""
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError as exc:
            raise json.JSONDecodeError(
                f"{exc.msg} (payload: {_payload_excerpt(result)})",
                exc.doc,
                exc.pos,
            ) from exc
    return result


def _payload_excerpt(payload: str) -> str:
    """Bound diagnostic excerpts without losing clues that distinguish HTML,
    throttling and empty bodies.
    """
    head = " ".join(payload.split())[:_PAYLOAD_EXCERPT_CHARS]
    if not head:
        return "empty"
    suffix = "..." if len(head) < len(payload.strip()) else ""
    return f"{head!r}{suffix}"


class MCPToolProvider:
    def __init__(
        self,
        mcp_client: MCPToolClient | None = None,
        *,
        run_id: str | None = None,
        corpus: str | None = None,
    ):
        self.mcp_client = mcp_client
        self._run_id = run_id
        self._corpus = corpus
        self._scope_fields: dict[str, set[str]] = {}

        self._tool_names: set[str] = set()

    def get_tools(
        self,
        mcp_whitelist: list[str] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """None exposes all tools; an empty whitelist exposes none."""
        tools_dict: dict[str, Any] = {}
        openai_tools: list[dict[str, Any]] = []

        # Forward None versus [] unchanged: all tools versus none.
        if self.mcp_client is not None:
            try:
                tools_dict, openai_tools = self.mcp_client.get_tools(whitelist=mcp_whitelist)
                self._tool_names.update(tools_dict.keys())
                for tool in openai_tools:
                    function = tool.get("function", {})
                    self._scope_fields[function.get("name", "")] = set(
                        function.get("parameters", {}).get("properties", {})
                    ) & {"run_id", "slug"}
                logger.debug("added %s MCP tools", len(tools_dict))
            except Exception as e:
                # Transient MCP outages degrade to no tools rather than aborting
                # the loop.
                logger.warning("Failed to get MCP tools: %s", e)

        logger.info("tool provider ready: %s tools", len(tools_dict))
        return tools_dict, openai_tools

    async def execute_tool_call(self, tool_call: Any) -> dict[str, Any]:
        """Reject unadvertised names; return failures as tool messages so the
        loop can continue.
        """
        tool_name = tool_call.function.name
        tool_call_id = tool_call.id

        # Reject names never advertised to the model.
        if tool_name not in self._tool_names:
            error_msg = f"unknown tool: {tool_name}"
            logger.error(error_msg)
            return tool_error_message(tool_name, tool_call_id, error_msg)

        try:
            if self.mcp_client is None:
                raise ConfigError("MCP client not configured")
            args = json.loads(tool_call.function.arguments)
            if not isinstance(args, dict):
                return tool_error_message(
                    tool_name, tool_call_id, "tool arguments must be an object"
                )
            original = dict(args)
            for field, expected in (("run_id", self._run_id), ("slug", self._corpus)):
                if field in args and args[field] not in (None, "", expected):
                    return tool_error_message(
                        tool_name, tool_call_id, "tool scope is fixed by the research task"
                    )
                if expected is not None and (
                    field in args or field in self._scope_fields.get(tool_name, set())
                ):
                    args[field] = expected
            if args != original:
                tool_call = SimpleNamespace(
                    id=tool_call_id,
                    function=SimpleNamespace(name=tool_name, arguments=json.dumps(args)),
                )
            return await self.mcp_client.execute_tool_call(tool_call)
        except Exception as e:
            # Return failures as tool messages so one bad call cannot abort the
            # multi-turn loop.
            error_msg = f"tool execution failed: {e!s}"
            logger.error("%s error: %s", tool_name, error_msg)
            return tool_error_message(tool_name, tool_call_id, error_msg)

    def tracked_executor(
        self,
        label: str,
    ) -> tuple[Callable[[Any], Awaitable[dict[str, Any]]], dict[str, int]]:
        return tracked_executor(self, label)
