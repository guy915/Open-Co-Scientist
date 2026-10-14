from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from co_scientist.core.constants import MINIMAL_REASONING_MAX_TOKENS
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    ToolLoop,
    call_llm_json,
    call_llm_with_tools,
)
from co_scientist.platform.llm.profile import model_profile
from co_scientist.platform.llm.roles import CallRole, current_call_policy, scoped_call_policy
from tests._azure_ledger import record_azure_allowance
from tests._llm_fake import (
    SEARCH_TOOL,
    echo_executor,
    install_fake_backend,
    make_completion,
    make_message,
    make_tool_call,
)

LUNA = "azure/gpt-6-luna-2026-09-22"


@pytest.mark.parametrize(
    ("role", "effort"),
    [
        ("reflection", "max"),
        ("generation", "max"),
        ("supervisor", "max"),
        ("overview_outline", "max"),
        ("overview_directions", "max"),
        ("chat", "medium"),
        ("interview", "medium"),
    ],
)
async def test_free_route_reasons_at_maximum_effort_except_conversation(
    monkeypatch: pytest.MonkeyPatch, role: CallRole, effort: str
) -> None:
    captured: list[dict[str, Any]] = []

    async def respond(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_completion(make_message('{"ok":1}'))

    install_fake_backend(monkeypatch, respond)
    assert await call_llm_json(
        "Answer",
        CompletionSpec(model_name="openrouter/inclusionai/ling-3.1-flash", role=role),
        max_attempts=1,
    ) == {"ok": 1}
    body = captured[0]["extra_body"]
    assert body["reasoning"] == {"enabled": True, "effort": effort}
    assert body["provider"]["max_price"] == {
        "prompt": 0.0,
        "completion": 0.0,
        "request": 0.0,
    }
    assert "reasoning_effort" not in captured[0]


async def test_reasoning_token_cap_is_scoped_to_one_public_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[int | None, dict[str, Any]]] = []

    async def respond(**kwargs: Any) -> Any:
        captured.append((current_call_policy().reasoning_max_tokens, kwargs))
        return make_completion(make_message('{"ok":1}'))

    install_fake_backend(monkeypatch, respond)
    spec = CompletionSpec(model_name=FREE)
    assert await call_llm_json(
        "Answer",
        spec,
        max_attempts=1,
        options=LLMCallOptions(reasoning_max_tokens=2048, prompt_name="kb_schema"),
    ) == {"ok": 1}
    assert await call_llm_json("Answer", spec, max_attempts=1) == {"ok": 1}

    assert [policy_cap for policy_cap, _ in captured] == [2048, None]
    assert captured[0][1]["extra_body"]["reasoning"] == {
        "enabled": True,
        "max_tokens": 2048,
    }
    assert captured[1][1]["extra_body"]["reasoning"] == {
        "enabled": True,
        "effort": "max",
    }


async def test_explicit_reasoning_cap_preserves_a_pinned_gateway_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.platform.llm.request import thinking

    captured: list[dict[str, Any]] = []

    async def respond(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_completion(make_message('{"ok":1}'))

    install_fake_backend(monkeypatch, respond)
    profile = dataclasses.replace(model_profile(FREE), pinned_effort="medium")
    monkeypatch.setattr(thinking, "model_profile", lambda _: profile)
    assert await call_llm_json(
        "Answer",
        CompletionSpec(model_name=FREE),
        max_attempts=1,
        options=LLMCallOptions(reasoning_max_tokens=2048),
    ) == {"ok": 1}

    assert captured[0]["extra_body"]["reasoning"] == {
        "enabled": True,
        "effort": "medium",
    }


async def test_rejected_reasoning_cap_recovers_once_with_minimal_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def respond(**kwargs: Any) -> Any:
        captured.append(kwargs)
        if len(captured) == 1:
            raise RuntimeError("reasoning max_tokens is not supported")
        return make_completion(make_message('{"ok":1}'))

    install_fake_backend(monkeypatch, respond)
    assert await call_llm_json(
        "Answer",
        CompletionSpec(model_name=FREE),
        max_attempts=2,
        options=LLMCallOptions(reasoning_max_tokens=2048),
    ) == {"ok": 1}

    assert len(captured) == 2
    assert captured[0]["extra_body"]["reasoning"] == {
        "enabled": True,
        "max_tokens": 2048,
    }
    assert captured[1]["extra_body"]["reasoning"] == {
        "enabled": True,
        "effort": "low",
    }


async def test_gateway_reasoning_cap_does_not_change_native_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def respond(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_completion(make_message('{"ok":1}'))

    install_fake_backend(monkeypatch, respond)
    assert await call_llm_json(
        "Answer",
        CompletionSpec(model_name=NATIVE),
        max_attempts=1,
        options=LLMCallOptions(reasoning_max_tokens=2048),
    ) == {"ok": 1}

    assert captured[0]["reasoning_effort"] == "high"
    assert captured[0]["extra_body"]["thinking"] == {"type": "enabled"}
    assert "max_tokens" not in captured[0]["extra_body"].get("reasoning", {})


@pytest.mark.parametrize(
    ("role", "effort"),
    [("generation", "medium"), ("ranking", "low"), ("overview_outline", "low"), ("chat", "medium")],
)
def test_paid_gateway_route_keeps_the_role_effort(role: CallRole, effort: str) -> None:
    from co_scientist.platform.llm.request.thinking import deepseek_thinking_extra_body

    with scoped_call_policy(role):
        body = deepseek_thinking_extra_body("openrouter/z-ai/glm-5.3-flash")
    assert body["reasoning"] == {"enabled": True, "effort": effort}


FREE = "openrouter/inclusionai/ling-3.1-flash"
PAID = "openrouter/z-ai/glm-5.3-flash"
NATIVE = "deepseek/deepseek-v4-flash"
HAIKU = "anthropic/claude-haiku-5-5"


def _wire(role: CallRole, model: str, *, enable_thinking: bool = True) -> dict[str, Any]:
    from co_scientist.platform.llm.request.thinking import (
        _apply_thinking_args,
        apply_provider_constraints,
    )

    args: dict[str, Any] = {"model": model, "max_tokens": 100, "messages": []}
    with scoped_call_policy(role, enable_thinking=enable_thinking):
        _apply_thinking_args(args, model, True)
        apply_provider_constraints(args, model)
    return args


def _reasoning(args: dict[str, Any]) -> object:
    extra = args.get("extra_body") or {}
    return (
        extra.get("reasoning"),
        extra.get("thinking"),
        args.get("reasoning_effort"),
        args.get("thinking"),
        args.get("output_config"),
    )


# Hosts that cannot switch reasoning off get the smallest cap instead.
_MINIMAL = {"enabled": True, "max_tokens": MINIMAL_REASONING_MAX_TOKENS}


@pytest.mark.parametrize(
    "role",
    ["goal_text", "announcement", "evidence_queries", "literature_queries", "research", "claims"],
)
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (FREE, (_MINIMAL, None, None, None, None)),
        (PAID, (_MINIMAL, None, None, None, None)),
        (NATIVE, (None, {"type": "disabled"}, None, None, None)),
        (LUNA, (None, None, "none", None, None)),
        (HAIKU, (None, None, None, {"type": "disabled"}, {"effort": "low"})),
    ],
)
def test_no_reasoning_roles_do_not_reason_on_any_provider(
    role: CallRole, model: str, expected: object
) -> None:
    assert _reasoning(_wire(role, model)) == expected


@pytest.mark.parametrize("role", ["relevance", "proximity", "research_extract", "safety"])
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (FREE, ({"enabled": True, "effort": "max"}, None, None, None, None)),
        (PAID, ({"enabled": True, "effort": "low"}, None, None, None, None)),
        (NATIVE, (None, {"type": "enabled"}, "high", None, None)),
        (LUNA, (None, None, "low", None, None)),
        (HAIKU, (None, None, None, {"type": "adaptive"}, {"effort": "low"})),
    ],
)
def test_reasoning_roles_reason_on_every_provider(
    role: CallRole, model: str, expected: object
) -> None:
    assert _reasoning(_wire(role, model)) == expected


@pytest.mark.parametrize("model", [FREE, PAID, NATIVE, LUNA, HAIKU])
def test_a_caller_turning_thinking_off_matches_a_no_reasoning_role(model: str) -> None:
    assert _reasoning(_wire("generation", model, enable_thinking=False)) == _reasoning(
        _wire("goal_text", model)
    )


def test_gateway_route_that_can_disable_reasoning_turns_it_off_for_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.platform.llm.request import thinking

    profile = model_profile(PAID)
    monkeypatch.setattr(
        thinking,
        "model_profile",
        lambda _: dataclasses.replace(profile, reasoning_can_disable=True),
    )
    assert _wire("goal_text", PAID)["extra_body"]["reasoning"] == {"enabled": False}


@pytest.fixture(autouse=True)
def funded_fake_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_AZURE_ENABLED", "true")
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "1")
    monkeypatch.setenv("LLM_AZURE_EXPIRES_AT", "2099-01-04T00:00:00+00:00")
    monkeypatch.setenv("LLM_USD_TO_EUR", "0.88")
    record_azure_allowance()


async def test_options_role_and_effort_survive_json_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = []

    async def respond(**kwargs: Any) -> Any:
        captured.append((current_call_policy(), kwargs))
        return make_completion(make_message('{"ok":"wrong"}' if len(captured) == 1 else '{"ok":1}'))

    install_fake_backend(monkeypatch, respond)
    result = await call_llm_json(
        "Answer",
        CompletionSpec(
            model_name=LUNA,
            role="worker",
            json_schema={
                "type": "object",
                "properties": {"ok": {"type": "integer"}},
                "required": ["ok"],
            },
        ),
        max_attempts=2,
        options=LLMCallOptions(role="overview_review", effort="low"),
    )
    assert result == {"ok": 1}
    assert len(captured) == 2
    assert all(
        policy.role == "overview_review" and policy.tier == "supervisor" for policy, _ in captured
    )
    assert all(args["reasoning_effort"] == "low" for _, args in captured)
    assert current_call_policy().role == "worker"


async def test_tool_turn_and_closing_harvest_keep_role_and_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []

    async def respond(**kwargs: Any) -> Any:
        captured.append((current_call_policy(), kwargs))
        if len(captured) == 1:
            return make_completion(
                make_message(None, tool_calls=[make_tool_call("one", "search", "{}")])
            )
        return make_completion(make_message("Observed result"))

    install_fake_backend(monkeypatch, respond)
    answer, _ = await call_llm_with_tools(
        "Answer",
        CompletionSpec(model_name=LUNA, role="drafting"),
        ToolLoop(SEARCH_TOOL, echo_executor, max_iterations=1),
        LLMCallOptions(effort="low", enable_thinking=False),
    )
    assert answer == "Observed result"
    assert len(captured) == 2
    assert all(policy.role == "drafting" and not policy.enable_thinking for policy, _ in captured)
    assert all(args["reasoning_effort"] == "none" for _, args in captured)
    assert "tools" in captured[0][1] and "tools" not in captured[1][1]


def test_role_effort_env_is_read_per_physical_request(monkeypatch: pytest.MonkeyPatch) -> None:
    with scoped_call_policy("overview_outline"):
        assert current_call_policy().effort == "low"
        monkeypatch.setenv("LLM_EFFORT_OVERVIEW_OUTLINE", "none")
        assert current_call_policy().effort == "none"
        monkeypatch.setenv("LLM_EFFORT_OVERVIEW_OUTLINE", "high")
        with pytest.raises(ProviderAdmissionError):
            current_call_policy()
    monkeypatch.setenv("LLM_WORKER_EFFORT", "none")
    with scoped_call_policy("claims"):
        assert current_call_policy().effort == current_call_policy().azure_effort == "none"


def test_reasoning_token_cap_must_be_positive() -> None:
    with (
        pytest.raises(ProviderAdmissionError, match="positive integer"),
        scoped_call_policy("worker", reasoning_max_tokens=0),
    ):
        pass


@pytest.mark.parametrize(
    ("role", "effort", "azure_effort"),
    [
        ("generation", "medium", "low"),
        ("review", "medium", "low"),
        ("ranking", "low", "low"),
        ("safety", "low", "low"),
        ("claims", "none", "none"),
        ("literature_queries", "none", "none"),
        ("relevance", "low", "low"),
        ("proximity", "low", "low"),
        ("research_extract", "low", "low"),
        ("goal_text", "none", "none"),
        ("supervisor", "medium", "medium"),
        ("overview", "medium", "medium"),
        ("meta_review", "medium", "low"),
        ("overview_review", "low", "low"),
        ("overview_outline", "low", "low"),
        ("overview_directions", "low", "low"),
    ],
)
async def test_azure_runs_luna_at_low_or_none_and_planning_and_report_at_medium(
    monkeypatch: pytest.MonkeyPatch, role: CallRole, effort: str, azure_effort: str
) -> None:
    captured: list[dict[str, Any]] = []

    async def respond(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_completion(make_message('{"ok":1}'))

    install_fake_backend(monkeypatch, respond)
    with scoped_call_policy(role):
        assert current_call_policy().effort == effort
    assert await call_llm_json(
        "Answer", CompletionSpec(model_name=LUNA, role=role), max_attempts=1
    ) == {"ok": 1}
    assert captured[0]["reasoning_effort"] == azure_effort


def test_azure_prices_include_cache_write_and_long_context_without_guessing_deployment() -> None:
    luna = model_profile(LUNA)
    assert luna.version == "2026-09-22"
    assert luna.price is not None and luna.price.long_context is not None
    assert luna.price.cache_write_usd_per_million == 0.125
    assert luna.price.long_context.cache_write_usd_per_million == 0.25
    assert model_profile("azure/gpt-5-nano-2025-08-07").price is None
    assert model_profile("azure/unmapped-deployment").price is None


def test_luna_charges_short_rates_up_to_its_published_boundary() -> None:
    from co_scientist.platform.llm.admission.spend import _model_rates, price_cost

    rates = {**_model_rates(LUNA), "fx": "1"}
    assert price_cost(rates, 272_000, 0, cached=0, written=0) == 27_200
    assert price_cost(rates, 272_001, 0, cached=0, written=0) == 54_401
    legacy = {name: value for name, value in rates.items() if not name.startswith("short_")}
    assert price_cost(legacy, 272_000, 0, cached=0, written=0) == 54_400


def test_express_estimate_uses_luna_short_rates_for_bounded_inputs() -> None:
    from decimal import Decimal

    from co_scientist.platform.llm.admission.spend import SpendConfig
    from co_scientist.platform.llm.routing import express_estimate

    azure = SpendConfig(10_000_000, Decimal("1"), float("inf"))
    config = {"max_llm_calls": 1200}
    assert express_estimate("g", config, azure) == 57 * 101_025 * 6 // 10 + 360_000
    long_goal = "g" * 200_000
    assert express_estimate(long_goal, config, azure) == -(-57 * 301_024 * 12 // 10) + 540_000
