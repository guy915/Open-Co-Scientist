import asyncio
import json
import logging
import os
import threading
import weakref
from typing import TYPE_CHECKING, Any, Optional, cast

from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection

from co_scientist.core.constants import truncate
from co_scientist.core.env_vars import parse_timeout_env
from co_scientist.core.exceptions import MCPToolTimeoutError

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolRegistry


def _resolve_availability_check_tool(
    tool_registry: Optional["ToolRegistry"],
) -> tuple[str | None, bool]:
    if tool_registry is None:
        # Keep the historical check when no registry is supplied.
        return "check_pubmed_available", False

    workflow = tool_registry.get_workflow("literature_review")
    if not workflow:
        return None, False

    if not workflow.availability_check:
        logger.debug("availability check disabled in config (availability_check: null)")
        return None, True

    tool_config = tool_registry.get_tool(workflow.availability_check)
    check_tool_name = tool_config.mcp_tool_name if tool_config else None
    return check_tool_name, False


def _interpret_availability_result(result: Any, check_tool_name: str) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, str):
        return result.lower() == "true"
    logger.warning("unexpected result from %s: %s", check_tool_name, result)
    return False


def _short_circuit_availability(
    all_tools_dict: dict[str, Any],
    check_tool_name: str | None,
    skip_availability_check: bool,
) -> bool | None:
    if not all_tools_dict:
        logger.warning("MCP server responded but provided no tools, literature source unavailable")
        return False

    if skip_availability_check or check_tool_name is None:
        logger.info(
            "MCP server available, skipping source-specific availability check"
            if skip_availability_check
            else "no availability check tool configured, assuming source available"
        )
        return True

    return None


async def _call_check_tool(
    mcp_client: "MCPToolClient",
    check_tool_name: str | None,
    all_tools_dict: dict[str, Any],
) -> bool:
    if check_tool_name not in all_tools_dict:
        logger.warning(
            "availability check tool '%s' not found. available tools: %s",
            check_tool_name,
            list(all_tools_dict.keys()),
        )
        return False

    logger.debug("%s tool found, executing", check_tool_name)
    result = await mcp_client.call_tool(check_tool_name)
    return _interpret_availability_result(result, check_tool_name)


async def _probe_literature_source_availability(
    mcp_client: "MCPToolClient",
    check_tool_name: str | None,
    skip_availability_check: bool,
) -> bool:
    all_tools_dict, _ = mcp_client.get_tools()

    shortcut = _short_circuit_availability(all_tools_dict, check_tool_name, skip_availability_check)
    if shortcut is not None:
        return shortcut

    logger.debug("checking literature source availability (tool: %s)", check_tool_name)
    logger.debug("available mcp tools: %s", list(all_tools_dict.keys()))

    return await _call_check_tool(mcp_client, check_tool_name, all_tools_dict)


def _log_mcp_test_start(tool_registry: Optional["ToolRegistry"], server_url: str | None) -> None:
    if tool_registry:
        logger.debug(
            "testing mcp availability for %s server(s)",
            len(tool_registry.get_enabled_servers()),
        )
    else:
        logger.debug("testing mcp server availability at %s", server_url)


def _has_any_tools(tools_dict: dict[str, Any] | None) -> bool:
    if tools_dict and len(tools_dict) > 0:
        logger.info("MCP server available with %s tools", len(tools_dict))
        return True
    logger.warning("MCP server responded but provided no tools")
    return False


def _log_mcp_unavailable(
    tool_registry: Optional["ToolRegistry"],
    server_url: str | None,
    error: Exception,
) -> None:
    if tool_registry:
        logger.warning("MCP servers unavailable: %s", error)
    else:
        logger.warning("MCP server unavailable at %s", server_url)


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolRegistry


DEFAULT_MCP_SERVER_URL = "http://localhost:8888/mcp"

MCP_SHARED_SECRET_ENV = "COSCIENTIST_MCP_SHARED_SECRET"
# API and MCP must share the same secret; network placement alone is not
# authentication.

MCP_AUTH_HEADER = "X-MCP-Shared-Secret"


def _resolve_server_url() -> str:
    return os.environ.get("MCP_SERVER_URL", DEFAULT_MCP_SERVER_URL)


def _mcp_auth_headers() -> dict[str, Any] | None:
    secret = os.environ.get(MCP_SHARED_SECRET_ENV)
    if not secret:
        return None
    return {MCP_AUTH_HEADER: secret}


def _with_shared_secret(
    configs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    headers = _mcp_auth_headers()
    if headers is None:
        return configs
    return {
        server_id: {
            **cfg,
            "headers": {**(cfg.get("headers") or {}), **headers},
        }
        for server_id, cfg in configs.items()
    }


def _resolve_server_configs(
    tool_registry: Optional["ToolRegistry"],
    server_configs: dict[str, dict[str, Any]] | None,
    server_url: str | None,
) -> dict[str, dict[str, Any]]:
    if tool_registry is not None:
        configs = tool_registry.get_server_configs_for_langchain()
        logger.debug("using %s servers from tool registry", len(configs))
        return _with_shared_secret(configs)

    if server_configs is not None:
        logger.debug("using %s provided server configs", len(server_configs))
        return _with_shared_secret(server_configs)

    if server_url is None:
        server_url = _resolve_server_url()
    logger.debug("using single server: %s", server_url)
    return _with_shared_secret({"default": {"transport": "streamable_http", "url": server_url}})


NOT_INITIALIZED_MESSAGE = "mcp client not initialized. call initialize() first."


def _ensure_tools_initialized(
    tools_dict: dict[str, Any] | None,
) -> dict[str, Any]:
    if tools_dict is None:
        raise RuntimeError(NOT_INITIALIZED_MESSAGE)
    return tools_dict


def _is_wrapped_text_result(result: Any) -> bool:
    """Some LangChain versions wrap MCP results in a list of text blocks."""
    return (
        isinstance(result, list)
        and len(result) > 0
        and isinstance(result[0], dict)
        and "text" in result[0]
    )


def _unwrap_tool_result(result: Any) -> Any:
    return result[0]["text"] if _is_wrapped_text_result(result) else result


def _filter_tools_by_whitelist(
    tools_dict: dict[str, Any],
    whitelist: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Advertise tools in workflow whitelist order, preserving configured
    preference.
    """
    filtered_tools_dict = {k: v for k, v in tools_dict.items() if k in whitelist}
    filtered_openai_tools = [
        convert_to_openai_tool(filtered_tools_dict[k])
        for k in whitelist
        if k in filtered_tools_dict
    ]

    logger.debug(
        "filtered to %s tools: %s",
        len(filtered_tools_dict),
        list(filtered_tools_dict.keys()),
    )

    return filtered_tools_dict, filtered_openai_tools


MCP_TOOL_TIMEOUT_ENV = "COSCIENTIST_MCP_TOOL_TIMEOUT_SECONDS"
# A broken MCP stream can leave the await pending forever.
DEFAULT_MCP_TOOL_TIMEOUT_SECONDS = 300.0


def mcp_tool_timeout_seconds() -> float | None:
    """Read the ceiling per call so operators can change it without
    restarting.
    """
    return parse_timeout_env(MCP_TOOL_TIMEOUT_ENV, DEFAULT_MCP_TOOL_TIMEOUT_SECONDS)


async def _ainvoke_within_timeout(tool: Any, tool_args: Any, tool_name: str) -> Any:
    timeout = mcp_tool_timeout_seconds()
    if timeout is None:
        return await tool.ainvoke(tool_args)
    try:
        return await asyncio.wait_for(tool.ainvoke(tool_args), timeout=timeout)
    except asyncio.TimeoutError as exc:
        # wait_for cancels the broken stream before reporting the timeout.
        raise MCPToolTimeoutError(
            f"MCP tool '{tool_name}' did not respond within {timeout}s"
        ) from exc


class UnknownToolError(ValueError):
    """The server never registered the tool (web search without a key), so
    asking again cannot succeed."""


class MCPToolClient:
    def __init__(
        self,
        server_url: str | None = None,
        server_configs: dict[str, dict[str, Any]] | None = None,
        tool_registry: Optional["ToolRegistry"] = None,
    ):
        self._tool_registry = tool_registry
        self._client: MultiServerMCPClient | None = None
        self._tools_dict: dict[str, Any] | None = None
        self._openai_tools: list[dict[str, Any]] | None = None
        self._initialize_lock = asyncio.Lock()
        self._tool_to_server: dict[str, str] = {}

        self._server_configs = _resolve_server_configs(tool_registry, server_configs, server_url)

        self.server_url = (
            next(iter(self._server_configs.values())).get("url") if self._server_configs else None
        )

    async def initialize(self) -> None:
        # Tool indexes define readiness; concurrent callers must never see a
        # half-initialized transport.
        if self._tools_dict is not None:
            logger.debug("MCP client already initialized")
            return

        async with self._initialize_lock:
            if self._tools_dict is not None:
                logger.debug("MCP client initialized by concurrent caller")
                return
            await self._initialize_locked()

    async def _initialize_locked(self) -> None:
        if not self._server_configs:
            raise RuntimeError("no server configurations available")

        server_names = list(self._server_configs.keys())
        logger.info(
            "initializing MCP client for %s server(s): %s",
            len(server_names),
            server_names,
        )

        client = MultiServerMCPClient(
            cast(dict[str, Connection], self._server_configs), handle_tool_errors=False
        )
        # Publish only after every server has yielded complete tool indexes.
        tools = await client.get_tools()
        self._client = client
        self._index_tools(tools)

        assert self._tools_dict is not None
        logger.info(
            "MCP client initialized with %s tools: %s",
            len(self._tools_dict),
            list(self._tools_dict.keys()),
        )

    def _index_tools(self, tools: list[Any]) -> None:
        self._tools_dict = {}
        self._tool_to_server = {}

        for tool in tools:
            self._tools_dict[tool.name] = tool
            # Some MultiServerMCPClient versions prefix tool names; use registry
            # ownership when available.
            if self._tool_registry:
                tool_config = self._tool_registry.get_tool_by_mcp_name(tool.name)
                if tool_config:
                    self._tool_to_server[tool.name] = tool_config.server

        self._openai_tools = [convert_to_openai_tool(tool) for tool in tools]

    @staticmethod
    def _require_tool(tools_dict: dict[str, Any], tool_name: str) -> Any:
        if tool_name not in tools_dict:
            raise UnknownToolError(
                f"tool '{tool_name}' not found. available tools: {list(tools_dict.keys())}"
            )
        return tools_dict[tool_name]

    async def call_tool(self, tool_name: str, **kwargs: Any) -> str:
        tools_dict = _ensure_tools_initialized(self._tools_dict)
        tool = self._require_tool(tools_dict, tool_name)

        logger.debug("calling mcp tool: %s with args: %s", tool_name, kwargs)

        # Direct callers degrade per source, so a dead tool raises rather than
        # poisoning the whole run.
        result = _unwrap_tool_result(await _ainvoke_within_timeout(tool, kwargs, tool_name))

        logger.debug(
            "mcp tool result for %s: %s",
            tool_name,
            truncate(str(result)),
        )

        return cast(str, result)

    @staticmethod
    async def _invoke_or_timeout_result(
        tool: Any, tool_args: dict[str, Any], tool_name: str
    ) -> str:
        """Return timeouts as results: raising inside gather would cancel
        useful sibling calls.
        """
        try:
            return cast(
                str,
                _unwrap_tool_result(await _ainvoke_within_timeout(tool, tool_args, tool_name)),
            )
        except MCPToolTimeoutError as exc:
            logger.warning("%s; reporting the timeout to the model", exc)
            return f"Error: {exc}. No result was returned."

    async def execute_tool_call(self, tool_call: Any) -> dict[str, Any]:
        tools_dict = _ensure_tools_initialized(self._tools_dict)
        tool_name = tool_call.function.name
        tool_args = json.loads(tool_call.function.arguments)

        logger.debug("executing mcp tool: %s with args: %s", tool_name, tool_args)
        result = await self._invoke_or_timeout_result(tools_dict[tool_name], tool_args, tool_name)
        logger.debug(
            "mcp tool result for %s: %s%s",
            tool_name,
            str(result)[:200],
            "..." if len(str(result)) > 200 else "",
        )

        return {
            "role": "tool",
            "name": tool_name,
            "tool_call_id": tool_call.id,
            "content": result,
        }

    # Advertise only tools authorized by the workflow's whitelist.
    def get_tools(
        self, whitelist: list[str] | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        tools_dict = _ensure_tools_initialized(self._tools_dict)
        # The converted schema list independently guards against half-
        # initialized state.
        if self._openai_tools is None:
            raise RuntimeError(NOT_INITIALIZED_MESSAGE)

        if whitelist is None:
            return tools_dict, self._openai_tools

        return _filter_tools_by_whitelist(tools_dict, whitelist)

    def has_tool(self, tool_name: str) -> bool:
        if self._tools_dict is None:
            return False
        return tool_name in self._tools_dict


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolRegistry


# Separate worker loops own separate asyncio locks/sessions and captured
# headers.
# Weak keys release clients with their loop.
_global_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Any, MCPToolClient]] = (
    weakref.WeakKeyDictionary()
)
_global_clients_lock = threading.Lock()


def _freeze_config(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze_config(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_config(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze_config(item) for item in value))
    try:
        hash(value)
    except TypeError:
        return (type(value), id(value))
    return value


async def _probe_literature_source(
    server_url: str | None,
    tool_registry: Optional["ToolRegistry"],
    check_tool_name: str | None,
    skip_availability_check: bool,
) -> bool:
    try:
        # Use a throwaway client so outages cannot poison the cached client.
        mcp_client = MCPToolClient(server_url=server_url, tool_registry=tool_registry)
        await mcp_client.initialize()
        return await _probe_literature_source_availability(
            mcp_client, check_tool_name, skip_availability_check
        )
    except Exception as e:
        # Availability is a capability gate; any probe failure must degrade
        # instead of aborting science.
        logger.warning(
            "error checking literature source availability: %s: %s",
            type(e).__name__,
            e,
        )
        logger.debug("full traceback: %s", e, exc_info=True)
        return False


async def check_literature_source_available(
    server_url: str | None = None,
    tool_registry: Optional["ToolRegistry"] = None,
) -> bool:
    check_tool_name, skip_availability_check = _resolve_availability_check_tool(tool_registry)

    if server_url is None and tool_registry is None:
        server_url = _resolve_server_url()

    return await _probe_literature_source(
        server_url, tool_registry, check_tool_name, skip_availability_check
    )


# API and MCP deploy independently; older servers expose presence without the
# reachability check.
WEB_SEARCH_CHECK_TOOL = "check_web_search_available"
WEB_SEARCH_TOOL = "search_web"


async def check_web_search_available(
    server_url: str | None = None,
    tool_registry: Optional["ToolRegistry"] = None,
) -> bool:
    """A configured key may be refused; probe provider reachability, with
    presence fallback for older servers.
    """
    if server_url is None and tool_registry is None:
        server_url = _resolve_server_url()

    try:
        probe_client = MCPToolClient(server_url=server_url, tool_registry=tool_registry)
        await probe_client.initialize()
        tools, _ = probe_client.get_tools()
        if WEB_SEARCH_CHECK_TOOL in tools:
            return await _call_check_tool(probe_client, WEB_SEARCH_CHECK_TOOL, tools)
        return WEB_SEARCH_TOOL in tools
    except Exception as e:
        # Unreachable means unusable now, not a failed scientific run.
        logger.debug("web search availability check failed: %s", e)
        return False


async def check_mcp_available(
    server_url: str | None = None,
    tool_registry: Optional["ToolRegistry"] = None,
) -> bool:
    if server_url is None and tool_registry is None:
        server_url = _resolve_server_url()

    try:
        _log_mcp_test_start(tool_registry, server_url)

        test_client = MCPToolClient(server_url=server_url, tool_registry=tool_registry)
        await test_client.initialize()

        tools_dict = test_client._tools_dict
        return _has_any_tools(tools_dict)

    except Exception as e:
        # Probe failures disable retrieval instead of aborting the run.
        _log_mcp_unavailable(tool_registry, server_url, e)
        return False


async def get_mcp_client(
    server_url: str | None = None,
    tool_registry: Optional["ToolRegistry"] = None,
    force_new: bool = False,
) -> MCPToolClient:
    configs = _resolve_server_configs(tool_registry, None, server_url)
    key = _freeze_config(configs)
    loop = asyncio.get_running_loop()
    with _global_clients_lock:
        for cached_loop in list(_global_clients):
            if cached_loop.is_closed():
                del _global_clients[cached_loop]
        loop_clients = _global_clients.setdefault(loop, {})
        if force_new or key not in loop_clients:
            loop_clients[key] = MCPToolClient(server_url=server_url, tool_registry=tool_registry)

    client = loop_clients[key]
    await client.initialize()

    return client
