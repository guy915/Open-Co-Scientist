import contextlib
import logging
from dataclasses import dataclass
from typing import Any, cast

from co_scientist.core.exceptions import (
    LLMBudgetExhaustedError,
    LLMContentFilteredError,
    LLMThinkingOnlyError,
)
from co_scientist.platform.llm.profile import model_profile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenUsage:
    """Cache reads are a discounted slice of prompt tokens, not an additional
    charge.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_tokens: int = 0


def extract_token_usage(response: Any) -> TokenUsage:
    """Missing telemetry must not turn a healthy response into a request
    failure.
    """
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None) or 0
    completion_tokens = getattr(usage, "completion_tokens", None) or 0
    details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = getattr(details, "reasoning_tokens", None) or 0
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(prompt_details, "cached_tokens", None) or 0
    written = getattr(prompt_details, "cache_write_tokens", None)
    if written is None:
        written = getattr(prompt_details, "cache_creation_tokens", None) or 0
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_prompt_tokens=min(cached, prompt_tokens),
        cache_write_tokens=written,
    )


def _empty_content_diagnosis(response: Any) -> str:
    """Raw responses duplicate reasoning and flood the bounded Logs feed.
    Diagnostics must not mask the original failure.
    """
    parts: list[str] = []

    with contextlib.suppress(Exception):
        parts.append(f"finish_reason={response.choices[0].finish_reason}")

    usage = getattr(response, "usage", None)
    for field in ("prompt_tokens", "completion_tokens"):
        value = getattr(usage, field, None)
        if value is not None:
            parts.append(f"{field}={value}")

    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None)
    if reasoning is not None:
        parts.append(f"reasoning_tokens={reasoning}")

    return ", ".join(parts) if parts else "no usage reported"


def _finish_reason(response: Any) -> str | None:
    """Unreadable diagnostic fields must not replace the original response
    failure.
    """
    with contextlib.suppress(Exception):
        return cast("str | None", response.choices[0].finish_reason)
    return None


def is_model_refusal(response: Any) -> bool:
    # The pinned SDK maps Anthropic's native refusal to content_filter.
    return getattr(response, "stop_reason", None) == "refusal" or _finish_reason(response) in (
        "refusal",
        "content_filter",
    )


def _served_model(response: Any) -> str | None:
    served = getattr(response, "model", None)
    if not isinstance(served, str) or not served.strip():
        return None
    return served.strip()


def _model_label(response: Any, model_name: str) -> str:
    served = _served_model(response)
    return f"{model_name}, served {served}" if served else model_name


def _base_route(name: str) -> str:
    return name.removeprefix("openrouter/").partition(":")[0]


def _log_fallback_answer(response: Any, model_name: str) -> None:
    """Later parse failures carry only text; this names the fallback that
    produced it.
    """
    served = _served_model(response)
    if served is None:
        return
    fallbacks = {_base_route(f) for f in model_profile(model_name).fallbacks}
    if _base_route(served) in fallbacks:
        logger.info("LLM call to %s was answered by fallback %s", model_name, served)


def _extract_completion_content(response: Any, model_name: str) -> str:
    _log_fallback_answer(response, model_name)
    if is_model_refusal(response):
        raise LLMContentFilteredError("The provider refused the response.")
    content = response.choices[0].message.content
    if isinstance(content, list):
        texts = []
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if kind == "text" and isinstance(text, str):
                texts.append(text)
        content = "".join(texts)

    if not isinstance(content, str) or not content.strip():
        # The attempt boundary logs once with retry context; logging extraction
        # failures here duplicates it.
        raise _empty_content_error(response, model_name, _empty_content_diagnosis(response))

    return content


def _empty_content_error(response: Any, model_name: str, diagnosis: str) -> ValueError:
    """
    Provider aborts precede reasoning spend: the model did not choose to stop.
    Only normal thinking-only stops skip the larger-budget rung.
    """
    finish_reason = _finish_reason(response)
    model = _model_label(response, model_name)
    if finish_reason == "length":
        return LLMBudgetExhaustedError(
            f"LLM spent its entire token budget without answering. Model: {model} ({diagnosis})"
        )
    if finish_reason == "error":
        return ValueError(
            "LLM provider reported an error mid-stream and wrote no "
            f"answer. Model: {model} ({diagnosis})"
        )
    # A filter verdict is not a reasoning-budget failure; shrinking reasoning
    # cannot answer it.
    if finish_reason == "content_filter":
        return LLMContentFilteredError(
            f"LLM provider content filter withheld the answer. Model: {model} ({diagnosis})"
        )
    if extract_token_usage(response).reasoning_tokens > 0:
        return LLMThinkingOnlyError(
            f"LLM finished its chain of thought and wrote no answer. Model: {model} ({diagnosis})"
        )
    return ValueError(f"LLM returned None or empty content. Model: {model}")
