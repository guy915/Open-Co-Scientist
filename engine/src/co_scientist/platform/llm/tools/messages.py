import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


def tool_result_message(tool_name: str, tool_call_id: str, payload: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "name": tool_name,
        "tool_call_id": tool_call_id,
        "content": json.dumps(payload, default=str),
    }


def tool_error_message(tool_name: str, tool_call_id: str, error: str) -> dict[str, Any]:
    return tool_result_message(tool_name, tool_call_id, {"error": error})


def tracked_executor(
    provider: Any, label: str
) -> tuple[Callable[[Any], Awaitable[dict[str, Any]]], dict[str, int]]:
    counts: dict[str, int] = {}

    async def executor(tool_call: Any) -> dict[str, Any]:
        name = tool_call.function.name
        counts[name] = counts.get(name, 0) + 1
        logger.info("%s: %s call #%s", label, name, counts[name])
        result: dict[str, Any] = await provider.execute_tool_call(tool_call)
        return result

    return executor, counts
