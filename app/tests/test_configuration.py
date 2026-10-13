from __future__ import annotations

from typing import Any

from co_scientist.core.config import BYOK_PROVIDER_DEFAULT_MODELS, Settings
from co_scientist.platform.llm.profile import MODEL_PRICING

# Unknown model pricing silently reports zero spend and removes gateway price
# caps.


_MODEL_FIELDS = (
    "model_name",
    "supervisor_model_name",
    "chat_model_name",
    "semantic_safety_model",
)


def _default_models() -> set[str]:
    # Inspect shipped field defaults rather than dotenv-overridden live
    # settings.
    return {
        default
        for field in _MODEL_FIELDS
        if isinstance(default := Settings.model_fields[field].default, str)
    }


def test_every_default_and_byok_model_is_priced() -> None:
    unpriced = sorted(
        (_default_models() | set(BYOK_PROVIDER_DEFAULT_MODELS.values())) - set(MODEL_PRICING)
    )

    assert not unpriced, f"models missing from MODEL_PRICING: {unpriced}"


def test_the_free_default_route_reasons_at_medium_effort_in_chat() -> None:
    from co_scientist.core.config import DEFAULT_MODEL
    from co_scientist.platform.llm.request.thinking import _apply_thinking_args
    from co_scientist.platform.llm.roles import scoped_call_policy

    kwargs: dict[str, Any] = {"model": DEFAULT_MODEL, "max_tokens": 100}
    with scoped_call_policy("chat"):
        _apply_thinking_args(kwargs, DEFAULT_MODEL, True)

    assert kwargs["extra_body"]["reasoning"]["effort"] == "medium"
