"""Leaf value definitions avoid import cycles between dispatch layers."""

from dataclasses import dataclass
from typing import Any

from co_scientist.core.constants import DEFAULT_MAX_TOKENS, HIGH_TEMPERATURE
from co_scientist.platform.llm.roles import CallRole, ReasoningEffort


@dataclass(frozen=True)
class CompletionSpec:
    """Provider credentials must never enter persisted request state."""

    model_name: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = HIGH_TEMPERATURE
    json_schema: dict[str, Any] | None = None
    force_json: bool = False
    api_key: str | None = None
    role: CallRole = "worker"
    effort: ReasoningEffort | None = None


@dataclass(frozen=True)
class LLMCallOptions:
    """Attempt loops own the failure log because they know retry context."""

    run_id: str | None = None
    prompt_name: str | None = None
    enable_thinking: bool = True
    role: CallRole | None = None
    effort: ReasoningEffort | None = None
    reasoning_max_tokens: int | None = None


@dataclass(frozen=True)
class LLMRequest:
    """Advertised tool schemas do not identify external tool behavior;
    optional tool_contract does.
    """

    prompt: str
    model_name: str
    temperature: float
    max_tokens: int
    tools: list[dict[str, Any]] | None = None
    json_schema: dict[str, Any] | None = None
    force_json: bool | None = None
    tool_contract: dict[str, Any] | None = None


def indexed_prompt_name(stem: str, index: int | None) -> str:
    return f"{stem}_{index}" if index is not None else stem
