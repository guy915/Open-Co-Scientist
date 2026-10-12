from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from co_scientist.orchestration.generator.core import HypothesisGenerator
from co_scientist.orchestration.generator.run_setup import GeneratorOptions
from co_scientist.platform.llm import (
    CompletionSpec,
    ToolLoop,
    call_llm,
    call_llm_json,
    call_llm_with_tools,
    deepseek_thinking_extra_body,
    model_profile,
    scoped_api_key,
)
from co_scientist.platform.llm.profile import (
    MODEL_PRICING,
    gateway_routes,
    priced_routes,
)
from co_scientist.platform.llm.request.thinking import (
    _GATEWAY_MAX_FALLBACKS,
    _gateway_provider,
)
from tests._llm_fake import (
    install_fake_backend,
)


def _reply(content: str = "ok") -> SimpleNamespace:
    message = SimpleNamespace(role="assistant", content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.parametrize("kind", ["text", "json", "tools"])
@pytest.mark.parametrize("source", ["spec", "scoped", "none"])
async def test_a_byok_key_reaches_the_provider_and_is_never_invented(
    monkeypatch: pytest.MonkeyPatch, kind: str, source: str
) -> None:
    sent: list[dict[str, Any]] = []

    async def acompletion(**kwargs: Any) -> SimpleNamespace:
        sent.append(kwargs)
        return _reply('{"answer": 1}')

    async def executor(tool_call: Any) -> dict[str, Any]:
        return {}

    install_fake_backend(monkeypatch, acompletion)
    spec = CompletionSpec(
        model_name="openai/gpt-x",
        api_key="sk-byok-spec" if source == "spec" else None,
    )

    with scoped_api_key("sk-byok-scoped" if source == "scoped" else None):
        if kind == "text":
            await call_llm("prompt", spec)
        elif kind == "json":
            await call_llm_json("prompt", spec)
        else:
            await call_llm_with_tools(
                "prompt",
                spec,
                ToolLoop(tools=[], executor=executor, max_iterations=1),
            )

    assert (
        sent[0].get("api_key")
        == {
            "spec": "sk-byok-spec",
            "scoped": "sk-byok-scoped",
            "none": None,
        }[source]
    )
    assert ("api_key" in sent[0]) is (source != "none")


def test_a_byok_key_stays_out_of_state() -> None:
    """Workflow state is checkpointed wholesale; BYOK credentials must stay
    out."""
    generator = HypothesisGenerator(
        model_name="openai/gpt-x",
        options=GeneratorOptions(api_key="sk-byok-secret"),
    )

    assert generator.api_key == "sk-byok-secret"
    fields = generator._initial_config_fields()
    assert "sk-byok-secret" not in str(fields)
    assert "api_key" not in fields


def _price(model: str) -> tuple[float, float]:
    price = MODEL_PRICING[model]
    return price.prompt_usd_per_million, price.completion_usd_per_million


def test_no_fallback_costs_more_than_the_model_above_it() -> None:
    """Routine fallback from a free primary must not admit paid models."""
    for primary in gateway_routes():
        above = _price(primary)
        for name in model_profile(primary).fallbacks:
            below = _price(f"openrouter/{name}")
            assert below <= above, f"{name} falls back from {primary}"
            above = below


def test_every_catalogued_route_arms_the_routing_ceiling() -> None:
    for primary in gateway_routes():
        provider = _gateway_provider(primary)
        assert "max_price" in provider, primary
        if _price(primary) == (0, 0):
            assert provider["max_price"] == {
                "prompt": 0,
                "completion": 0,
                "request": 0,
            }


def test_every_declared_gateway_route_is_priced_and_funded() -> None:
    routes = gateway_routes()
    assert routes
    for name in routes:
        profile = model_profile(name)
        assert profile.price is not None, name
        assert profile.reasons, name


def test_no_declared_chain_exceeds_openrouters_fallback_cap() -> None:
    """OpenRouter accepts at most three models in a fallback chain."""
    for primary in gateway_routes():
        body = deepseek_thinking_extra_body(primary)
        fallbacks = model_profile(primary).fallbacks
        assert len(fallbacks) <= _GATEWAY_MAX_FALLBACKS, primary
        assert len(body.get("models", [])) <= _GATEWAY_MAX_FALLBACKS, primary


def test_no_chain_head_claims_disable_support_a_fallback_lacks() -> None:
    """Every fallback sees the same request and must honor its reasoning
    policy."""
    for primary in gateway_routes():
        declared = model_profile(primary)
        if not declared.reasoning_can_disable:
            continue
        for name in declared.fallbacks:
            fallback = model_profile(f"openrouter/{name}")
            assert fallback.gateway and fallback.reasoning_can_disable, (
                f"{primary} claims reasoning_can_disable=True but its fallback {name} does not"
            )


def test_the_price_table_is_the_profile_prices() -> None:
    assert priced_routes() == MODEL_PRICING
    for name, price in MODEL_PRICING.items():
        assert model_profile(name).price == price, name


def test_generic_profile_is_scoped_to_the_exact_byok_model() -> None:
    from co_scientist.core.byok_scope import ByokCredential, CustomModelCapabilities, scoped_byok

    name = "openrouter/vendor/new-model"
    caps = CustomModelCapabilities(context_length=131072, reasoning=True)
    credential = ByokCredential("openrouter", "synthetic-key", name, custom_models={name: caps})
    before = model_profile(name)
    with scoped_byok(credential):
        profile = model_profile(name)
        assert profile.context_length == 131072
        assert profile.tool_calling is True
        assert profile.gateway is True
        assert profile.reasons is True
        assert profile.json_schema is False
        assert profile.fallbacks == ()
        assert profile.price is None
    assert model_profile(name) == before


def test_generic_profile_preserves_curated_facts_and_defaults_conservatively() -> None:
    from co_scientist.core.byok_scope import ByokCredential, CustomModelCapabilities, scoped_byok
    from co_scientist.platform.llm.profile import generic_model_profile

    name = "deepseek/deepseek-flash"
    before = model_profile(name)
    caps = CustomModelCapabilities()
    with scoped_byok(ByokCredential("deepseek", "synthetic-key", name, custom_models={name: caps})):
        assert model_profile(name) == before
    fallback = generic_model_profile("openai/unknown", caps)
    assert fallback.context_length == 32768
    assert fallback.json_schema is False
    assert fallback.json_object is False
    assert fallback.reasoning_can_disable is False


def test_schema_capability_does_not_leak_between_custom_byok_scopes() -> None:
    from co_scientist.core.byok_scope import ByokCredential, CustomModelCapabilities, scoped_byok
    from co_scientist.platform.llm.request.backend import litellm_supports_json_schema

    model = "openrouter/vendor/scoped-model"
    for supported in (True, False, True):
        credential = ByokCredential(
            "openrouter",
            "synthetic-key",
            model,
            custom_models={model: CustomModelCapabilities(json_schema=supported)},
        )
        with scoped_byok(credential):
            assert litellm_supports_json_schema(model) is supported
