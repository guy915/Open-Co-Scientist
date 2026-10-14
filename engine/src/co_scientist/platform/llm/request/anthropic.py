from __future__ import annotations

import os
from copy import deepcopy
from typing import Any, cast

import httpx
import litellm
import openai
from litellm.exceptions import APIError as LiteLLMAPIError
from litellm.llms.anthropic.chat.transformation import AnthropicConfig
from litellm.types.llms.openai import AllMessageValues

from co_scientist.platform.db.anthropic_credit import (
    AnthropicCreditUnavailableError,
    AnthropicPromptTooLongError,
)
from co_scientist.platform.db.spend import UNAVAILABLE
from co_scientist.platform.llm.admission.anthropic import require_credit_available
from co_scientist.platform.llm.request.cache import HAIKU
from co_scientist.platform.llm.roles import ROLE_DEFAULTS, current_call_policy

API_BASE = "https://api.anthropic.com"
INPUT_LIMIT = 100_000
_SHORT_ROLES = frozenset(
    (
        "orchestrator",
        "literature_queries",
        "drafting",
        "novelty",
        "ranking",
        "proximity",
        "relevance",
        "claims",
        "safety",
        "question_repair",
        "goal_text",
        "announcement",
        "credential_probe",
    )
)
_LONG_ROLES = frozenset(("overview", "meta_review", "literature_analysis"))
OUTPUT_LIMITS = {
    role: (8192 if role in _SHORT_ROLES else 32768 if role in _LONG_ROLES else 16384)
    for role in ROLE_DEFAULTS
}


class AnthropicSlotUnavailableError(AnthropicCreditUnavailableError):
    pass


def haiku_thinking(*, summarize: bool = False) -> dict[str, str]:
    # Haiku 5.5 has no fixed thinking budget: adaptive at low effort, or off.
    mode = "adaptive" if current_call_policy().enable_thinking else "disabled"
    return {"type": mode, **({"display": "summarized"} if summarize and mode == "adaptive" else {})}


def count_request(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("model") != HAIKU or request.get("api_base", API_BASE) != API_BASE:
        raise AnthropicSlotUnavailableError(UNAVAILABLE)
    if litellm.model_alias_map or litellm.model_fallbacks or request.get("extra_body"):
        raise AnthropicSlotUnavailableError(UNAVAILABLE)
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages or messages[-1].get("role") == "assistant":
        raise AnthropicSlotUnavailableError(UNAVAILABLE)
    config = AnthropicConfig()
    params = config.map_openai_params(
        non_default_params={
            key: request[key] for key in ("tools", "tool_choice", "max_tokens") if key in request
        },
        optional_params={},
        model="claude-haiku-5-5",
        drop_params=True,
    )
    params["thinking"] = haiku_thinking()
    body = config.transform_request(
        model="claude-haiku-5-5",
        # The SDK extracts system messages in place; counting must not alter
        # the request subsequently sent to the model.
        messages=cast(list[AllMessageValues], deepcopy(messages)),
        optional_params=params,
        litellm_params={"drop_params": True},
        headers={},
    )
    return {
        key: body[key]
        for key in ("model", "messages", "system", "tools", "tool_choice", "thinking")
        if key in body
    }


def is_credit_error(error: BaseException) -> bool:
    # Only transport errors can exhaust credit; generated text is untrusted.
    if not isinstance(error, (httpx.HTTPStatusError, openai.APIError, LiteLLMAPIError)):
        return False
    response = getattr(error, "response", None)
    text = str(error)
    if response is not None:
        if response.status_code == 402:
            return True
        try:
            detail = response.json().get("error") or {}
            if detail.get("type") == "billing_error":
                return True
            text = detail.get("message") or text
        except (ValueError, AttributeError):
            pass
    return (
        getattr(error, "status_code", None) == 402
        or getattr(error, "code", None) == "insufficient_quota"
        or any(
            marker in text.lower()
            for marker in (
                "credit balance is too low",
                "insufficient credits",
                "insufficient_quota",
            )
        )
    )


async def require_prompt_fits(request: dict[str, Any], path: str) -> int:
    require_credit_available(path)
    key = os.getenv("ANTHROPIC_API_KEY")
    if not isinstance(key, str) or not key:
        raise AnthropicSlotUnavailableError(UNAVAILABLE)
    request["api_key"] = key
    # This metadata endpoint is free and has a separate rate limit. It never
    # creates a model response or runs under a database writer.
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        response = await client.post(
            f"{API_BASE}/v1/messages/count_tokens",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json=count_request(request),
        )
        response.raise_for_status()
        count = response.json().get("input_tokens")
    if type(count) is not int or count < 0:
        raise AnthropicSlotUnavailableError(UNAVAILABLE)
    # Haiku bills a longer prompt at five times the input rate.
    if count > INPUT_LIMIT:
        raise AnthropicPromptTooLongError(UNAVAILABLE)
    return count


def output_limit() -> int:
    return OUTPUT_LIMITS[current_call_policy().role]
