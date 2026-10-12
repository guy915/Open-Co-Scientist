from typing import Any

from co_scientist.platform.llm.tool_effects import set_registry_lookup


def _registered_tool(mcp_tool_name: str) -> Any:
    # Lazy: loading the tool registry reads its YAML configuration.
    from co_scientist.platform.retrieval.config.registry import get_tool_registry

    return get_tool_registry().get_tool_by_mcp_name(mcp_tool_name)


set_registry_lookup(_registered_tool)
