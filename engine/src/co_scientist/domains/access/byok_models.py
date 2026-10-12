from __future__ import annotations

from co_scientist.core.config import BYOK_PROVIDER_DEFAULT_MODELS

WORKER_MODEL_HEADER = "X-LLM-Model"
SUPERVISOR_MODEL_HEADER = "X-LLM-Supervisor-Model"

_EXTRA_PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "anthropic/claude-opus-5-5",
        "anthropic/claude-fable-5-1",
    ),
    "deepseek": ("deepseek/deepseek-v4-pro",),
    "gemini": (
        "gemini/gemini-3.1-pro-preview",
        "gemini/gemini-3.1-flash-lite",
    ),
    "openai": ("openai/gpt-6-astra", "openai/gpt-6-luna"),
    "openrouter": (
        "openrouter/inclusionai/ling-3.1-flash",
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter/deepseek/deepseek-v4-flash",
        "openrouter/deepseek/deepseek-v4-pro",
    ),
}


class ByokModelError(ValueError):
    """An unoffered provider model is an invalid request, not a provider
    outage.
    """


def provider_models(provider: str) -> tuple[str, ...]:
    default = BYOK_PROVIDER_DEFAULT_MODELS.get(provider)
    if default is None:
        return ()
    return (default, *_EXTRA_PROVIDER_MODELS.get(provider, ()))


def model_catalog() -> dict[str, list[str]]:
    return {provider: list(provider_models(provider)) for provider in BYOK_PROVIDER_DEFAULT_MODELS}


def resolve_model_choice(
    provider: str, requested: str | None, *, api_key: str | None = None
) -> str:
    offered = provider_models(provider)
    choice = (requested or "").strip()
    if not choice:
        return offered[0]
    if choice not in offered:
        from co_scientist.domains.access.custom_models import validate_custom_model

        if not api_key:
            raise ByokModelError("Custom models require your own API key")
        validated = validate_custom_model(provider, choice, api_key)
        if not validated.supported:
            raise ByokModelError(validated.error or "This model is unsupported")
        return validated.model
    return choice
