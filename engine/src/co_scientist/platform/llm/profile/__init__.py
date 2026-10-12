from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Final, TypedDict

from co_scientist.core.byok_scope import CustomModelCapabilities, current_byok

HAIKU: Final = "anthropic/claude-haiku-5-5"


@dataclass(frozen=True)
class ModelPrice:
    """Reasoning uses the completion rate; zero cache-read price means
    unmeasured, not free.
    """

    prompt_usd_per_million: float = 0.0
    completion_usd_per_million: float = 0.0
    cached_prompt_usd_per_million: float = 0.0
    cache_write_usd_per_million: float = 0.0
    long_context: ModelPrice | None = None


class Thinking(enum.Enum):
    """Sending native DeepSeek knobs through the gateway can silently enable
    reasoning instead of disabling it.
    """

    NONE = "none"
    NATIVE = "native"
    GATEWAY = "gateway"


@dataclass(frozen=True)
class ModelProfile:
    """Reasoning spend and knob support differ; unknown answerless models
    still need funding.
    """

    reasons: bool = False
    thinking: Thinking = Thinking.NONE
    # Disabling requires evidence for every fallback, never just the head.
    reasoning_can_disable: bool = False
    gateway: bool = False
    fallbacks: tuple[str, ...] = ()
    verified_provider: str | None = None
    # Unsupported required formats hard-fail routing; registry answers can
    # drift.
    json_schema: bool | None = None
    # False when no host accepts response_format; the schema then rides in the
    # prompt alone.
    json_object: bool = True
    # Overrides every caller's tier, chat included.
    pinned_effort: str | None = None
    # Reasoning APIs reject sampling knobs and want the reasoning-aware cap.
    fixed_sampling: bool = False
    max_completion_tokens: bool = False
    # Chat Completions refuses function calling on GPT-6 Sol and Astra.
    responses_api: bool = False
    min_temperature: float | None = None
    price: ModelPrice | None = None
    version: str | None = None
    supported_efforts: tuple[str, ...] | None = None
    context_length: int | None = None
    tool_calling: bool | None = None


class Facts(TypedDict, total=False):
    reasons: bool
    thinking: Thinking
    reasoning_can_disable: bool
    gateway: bool
    fallbacks: tuple[str, ...]
    verified_provider: str | None
    json_schema: bool | None
    json_object: bool
    pinned_effort: str | None
    fixed_sampling: bool
    max_completion_tokens: bool
    responses_api: bool
    min_temperature: float | None
    price: ModelPrice | None
    version: str | None
    supported_efforts: tuple[str, ...] | None


@dataclass(frozen=True)
class Family:
    facts: Facts
    prefix: str = ""
    contains: str = ""

    def matches(self, name: str) -> bool:
        return name.startswith(self.prefix) and self.contains in name


# Static zero-token rates do not establish live availability or all charges.
_FREE: Final = ModelPrice(0.0, 0.0)


def _gateway(
    price: ModelPrice,
    *,
    fallbacks: tuple[str, ...] = (),
    json_schema: bool = False,
    verified_provider: str | None = None,
) -> Facts:
    """A required unsupported response format hard-fails routing; use proven
    endpoint capabilities.
    """
    return {
        "gateway": True,
        "thinking": Thinking.GATEWAY,
        "reasons": True,
        "reasoning_can_disable": False,
        "json_schema": json_schema,
        "price": price,
        "fallbacks": fallbacks,
        "verified_provider": verified_provider,
    }


# Fallbacks must be cheaper, never paid under a free primary, and within the
# gateway array cap.
ROUTES: Final[dict[str, Facts]] = {
    "azure/gpt-6-luna-2026-09-22": {
        "reasons": True,
        "responses_api": True,
        "json_schema": True,
        "fixed_sampling": True,
        "version": "2026-09-22",
        "supported_efforts": ("none", "low", "medium"),
        "price": ModelPrice(0.10, 0.50, 0.01, 0.125, ModelPrice(0.20, 0.75, 0.02, 0.25)),
    },
    # Retained explicit route: both Nex variants reason and need bounded-minimal
    # reasoning.
    "openrouter/nex-agi/nex-n2.5-pro:free": _gateway(_FREE),
    "openrouter/nex-agi/nex-n2.5-mini:free": _gateway(_FREE),
    # Only the pinned ModelRun provider has checked zero-price/retention terms.
    # Its Qwen route supports native schemas but rejects JSON object mode.
    "openrouter/qwen/qwen3.8-27b:free": _gateway(
        _FREE, json_schema=True, verified_provider="modelrun"
    ),
    # Retain this all-free chain for deployments explicitly choosing it.
    "openrouter/z-ai/glm-5.2:free": _gateway(
        _FREE,
        fallbacks=(
            "minimax/minimax-m3:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "nvidia/nemotron-3.5-lightning:free",
        ),
    ),
    # Free variants have per-model daily allowances; every fallback must remain
    # zero-priced.
    "openrouter/minimax/minimax-m3:free": _gateway(
        _FREE,
        fallbacks=(
            "nvidia/nemotron-3-super-120b-a12b:free",
            "google/gemma-4-31b-it:free",
            "minimax/minimax-m2.7:free",
        ),
    ),
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free": _gateway(_FREE),
    "openrouter/google/gemma-4-31b-it:free": _gateway(_FREE),
    "openrouter/minimax/minimax-m2.7:free": _gateway(_FREE),
    "openrouter/dots-studio/dots-3-note-preview:free": _gateway(_FREE),
    "openrouter/nvidia/nemotron-3.5-lightning:free": _gateway(_FREE),
    # A zero-priced trial without a ":free" id; zero-cost admission still
    # checks the live catalog price and expiry.
    "openrouter/inclusionai/ling-3.1-flash": {
        **_gateway(
            _FREE,
            fallbacks=(
                "nvidia/nemotron-3-ultra-550b-a55b:free",
                "nvidia/nemotron-3-super-120b-a12b:free",
            ),
        ),
        "json_object": False,
    },
    # Its only host, Nvidia, rejects every response_format.
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free": {
        **_gateway(
            _FREE,
            # Dots answers tool and JSON turns with raw function-call tokens.
            fallbacks=("nvidia/nemotron-3-super-120b-a12b:free",),
        ),
        "json_object": False,
    },
    # Historical promotional rates need revalidation before selecting this paid
    # route.
    "openrouter/z-ai/glm-5.3-flash": _gateway(
        ModelPrice(0.15, 0.50, 0.015),
        fallbacks=(
            "minimax/minimax-m3:free",
            "nvidia/nemotron-3.5-lightning:free",
        ),
    ),
    # Gemma supports JSON object mode only; exact endpoint evidence overrides
    # family defaults.
    "openrouter/google/gemma-4-26b-a4b-it:free": {"json_schema": False},
    "deepseek/deepseek-v4-flash": {"price": ModelPrice(0.44, 1.32)},
    # Peak-hour rate; off-peak billing is lower.
    "deepseek/deepseek-flash": {"price": ModelPrice(0.30, 1.20)},
    "deepseek/deepseek-v4-pro": {"price": ModelPrice(1.32, 3.96)},
    "deepseek/deepseek-chat": {"price": ModelPrice(0.44, 1.32)},
    "deepseek/deepseek-reasoner": {"price": ModelPrice(1.32, 3.96)},
    "gemini/gemini-2.5-flash": {"price": ModelPrice(0.30, 2.50)},
    "gemini/gemini-2.5-flash-lite": {"price": ModelPrice(0.10, 0.40)},
    "gemini/gemini-2.5-pro": {"price": ModelPrice(1.25, 10.00)},
    "gemini/gemini-3.1-flash-lite": {"price": ModelPrice(0.25, 1.50)},
    "gemini/gemini-3.1-pro-preview": {"price": ModelPrice(2.00, 12.00)},
    "gemini/gemini-3.8-flash": {"price": ModelPrice(0.75, 3.75)},
    # Gateway-host rates differ from first-party rates; prices estimate capped
    # hosts, not a bill.
    "openrouter/deepseek/deepseek-v4-flash": {"price": ModelPrice(0.083, 0.165, 0.017)},
    # Use full-precision host prices; the cheapest quantized host would exclude
    # full-precision routes.
    "openrouter/deepseek/deepseek-v4-flash-0731": {"price": ModelPrice(0.13, 0.28, 0.028)},
    "openrouter/deepseek/deepseek-v4-pro": {"price": ModelPrice(1.60, 3.20, 0.13)},
    "openai/gpt-4o": {"price": ModelPrice(2.50, 10.00)},
    # Azure uses OpenAI list prices; unpriced BYOK models falsely report free
    # usage.
    "azure/gpt-4o": {"price": ModelPrice(2.50, 10.00)},
    "openai/gpt-4o-mini": {"price": ModelPrice(0.15, 0.60)},
    "openai/gpt-6.1-sol": {"price": ModelPrice(2.00, 10.00)},
    "openai/gpt-6-astra": {"price": ModelPrice(10.00, 50.00)},
    "openai/gpt-6-luna": {"price": ModelPrice(0.10, 0.50)},
    "anthropic/claude-sonnet-4-5": {"price": ModelPrice(3.00, 15.00)},
    "anthropic/claude-sonnet-5-5": {"price": ModelPrice(2.00, 10.00)},
    "anthropic/claude-opus-5-5": {"price": ModelPrice(4.00, 20.00)},
    "anthropic/claude-fable-5-1": {"price": ModelPrice(10.00, 50.00)},
    "anthropic/claude-haiku-5-5": {
        "price": ModelPrice(0.10, 0.50, 0.01, 0.125, ModelPrice(0.50, 2.50, 0.05, 0.625)),
        "reasons": True,
        "fixed_sampling": True,
        "json_schema": False,
        "pinned_effort": "low",
    },
    "anthropic/claude-haiku-4-5": {"price": ModelPrice(1.00, 5.00)},
}


FAMILIES: Final[tuple[Family, ...]] = (
    # The DeepSeek API accepts json_object only despite the capability registry
    # claiming schemas.
    Family(
        contains="deepseek",
        facts={
            "reasons": True,
            "thinking": Thinking.NATIVE,
            "reasoning_can_disable": True,
            "json_schema": False,
        },
    ),
    # Gateway DeepSeek needs normalized reasoning and a routing price ceiling
    # across variable-price hosts.
    Family(
        prefix="openrouter/",
        contains="deepseek",
        facts={"thinking": Thinking.GATEWAY, "gateway": True},
    ),
    # Gemini 3 degrades below temperature 1.0.
    Family(contains="gemini-3", facts={"min_temperature": 1.0}),
    # These think by default at the provider's effort; nothing here sends an
    # effort knob, but their thinking still spends the output cap.
    Family(prefix="gemini/", contains="gemini-3", facts={"reasons": True}),
    Family(
        prefix="anthropic/claude-",
        contains="-5-",
        # LiteLLM forces a tool call for native schemas, which thinking
        # rejects.
        facts={"reasons": True, "fixed_sampling": True, "json_schema": False},
    ),
    Family(
        prefix="openai/gpt-6",
        facts={
            "reasons": True,
            "fixed_sampling": True,
            "max_completion_tokens": True,
            "responses_api": True,
        },
    ),
)


def model_profile(model_name: str) -> ModelProfile:
    """Uncached reads let table patches take effect without stale capability
    state.
    """
    lowered = model_name.lower()
    credential = current_byok()
    if lowered not in ROUTES and credential is not None:
        for name, metadata in credential.custom_models.items():
            if name.lower() == lowered and name in credential.keys_by_model():
                return generic_model_profile(name, metadata)
    facts: dict[str, Any] = {}
    for family in FAMILIES:
        if family.matches(lowered):
            facts.update(family.facts)
    facts.update(ROUTES.get(lowered, {}))
    return ModelProfile(**facts)


def generic_model_profile(model_name: str, metadata: CustomModelCapabilities) -> ModelProfile:
    gateway = model_name.startswith("openrouter/")
    return ModelProfile(
        context_length=metadata.context_length or 32768,
        tool_calling=metadata.tool_calling,
        json_schema=metadata.json_schema,
        json_object=metadata.json_object,
        reasons=metadata.reasoning,
        thinking=Thinking.GATEWAY if gateway and metadata.reasoning else Thinking.NONE,
        gateway=gateway,
        reasoning_can_disable=metadata.reasoning_can_disable,
    )


def is_free_route(model_name: str) -> bool:
    return ":free" in model_name or model_profile(model_name).price == _FREE


def gateway_routes() -> tuple[str, ...]:
    return tuple(name for name, facts in ROUTES.items() if facts.get("gateway"))


def priced_routes() -> dict[str, ModelPrice]:
    return {
        name: price for name, facts in ROUTES.items() if (price := facts.get("price")) is not None
    }


__all__ = [
    "ModelPrice",
    "ModelProfile",
    "Thinking",
    "gateway_routes",
    "is_free_route",
    "model_profile",
    "priced_routes",
]


# Pricing uses exact case-sensitive routes; capability lookup does not.
MODEL_PRICING: Final[dict[str, ModelPrice]] = priced_routes()


def estimate_cost_usd(
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_prompt_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Unknown exact routes remain untracked; measured cache-read rates price
    provider cache hits.
    """
    price = MODEL_PRICING.get(model_name)
    if price is None:
        return 0.0
    if model_name == "anthropic/claude-haiku-5-5" and prompt_tokens > 100_000:
        price = price.long_context or price
    cached = 0
    if price.cached_prompt_usd_per_million:
        cached = max(0, min(cached_prompt_tokens, prompt_tokens))
    written = max(0, cache_write_tokens) if price.cache_write_usd_per_million else 0
    # Claude's normalized prompt includes writes. Azure reports writes as a
    # separate charge; its conservative credit ledger keeps that distinction.
    uncached = max(0, prompt_tokens - cached)
    if model_name.startswith("anthropic/") and written:
        uncached = max(0, uncached - written)
    return (
        uncached / 1_000_000 * price.prompt_usd_per_million
        + cached / 1_000_000 * price.cached_prompt_usd_per_million
        + completion_tokens / 1_000_000 * price.completion_usd_per_million
        + written / 1_000_000 * price.cache_write_usd_per_million
    )
