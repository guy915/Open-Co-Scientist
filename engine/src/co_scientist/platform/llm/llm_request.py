from typing import Any

from co_scientist.core import byok_scope
from co_scientist.platform.llm import offline_guard
from co_scientist.platform.llm.llm_scope import app_call_scope, in_app_call_scope
from co_scientist.platform.llm.request.thinking import _apply_thinking_args
from co_scientist.platform.llm.request.transport import complete_request
from co_scientist.platform.llm.roles import CallRole, ReasoningEffort, scoped_call_policy


async def acompletion(
    *,
    call_role: CallRole = "chat",
    call_effort: ReasoningEffort | None = None,
    enable_thinking: bool = True,
    **kwargs: Any,
) -> Any:
    with scoped_call_policy(call_role, call_effort, enable_thinking=enable_thinking):
        if not in_app_call_scope():
            with app_call_scope("completion"):
                return await _complete(kwargs)
        return await _complete(kwargs)


async def _complete(kwargs: dict[str, Any]) -> Any:
    from co_scientist.core.config import settings

    user_key = byok_scope.current_byok() is not None
    if not user_key:
        offline_guard.require_remote_chat("chat completion")
    kwargs.setdefault("max_tokens", settings.app_llm_max_output_tokens)
    # Callers send the answer budget only; the role policy adds each provider's reasoning.
    _apply_thinking_args(kwargs, str(kwargs["model"]), True)
    timeout = kwargs.get("timeout")
    if kwargs.get("stream"):
        kwargs.setdefault("stream_options", {"include_usage": True})
    return await complete_request(
        kwargs,
        str(kwargs["model"]),
        byok=user_key,
        timeout_seconds=float(timeout) if timeout is not None else 600.0,
    )
