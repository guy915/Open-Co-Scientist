from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from jsonschema.exceptions import ValidationError
from litellm.exceptions import APIError, BadRequestError, RateLimitError
from litellm.exceptions import ContextWindowExceededError as ContextWindow

from co_scientist.core.exceptions import (
    FreeModelEligibilityError,
    LLMBudgetExhaustedError,
    LLMCallBudgetExceededError,
    LLMRateLimitParkError,
    LLMThinkingOnlyError,
    LLMTimeoutError,
)
from co_scientist.platform.llm import LLMCallOptions
from tests._llm_fake import (
    GATEWAY_MODEL,
    JSON,
    SCHEMA_FEEDBACK,
    TEXT,
    TOOLS,
    WAITED_THEN_GAVE_UP,
    Driver,
    Entry,
    call_ceiling,
    echo_executor,
    exhausted,
    make_completion,
    make_message,
    make_tool_call,
    ok,
    overloaded,
    rate_limited,
    reasoning_mandatory,
    thinking_only,
    timed_out,
    too_big,
    wrong_type,
)
from tests._llm_fake import drive as drive

__all__ = ["drive"]

EVERY_ENTRY = pytest.mark.parametrize(
    "entry", [TEXT, JSON, TOOLS], ids=["call_llm", "call_llm_json", "tool_turn"]
)
TEXT_AND_JSON = pytest.mark.parametrize("entry", [TEXT, JSON], ids=["call_llm", "call_llm_json"])


def _answer(entry: Entry, result: Any) -> Any:
    return result[0] if entry.kind == "tools" else result


def _expected(entry: Entry) -> Any:
    return {"a": 1} if entry.kind == "json" else "fine"


@EVERY_ENTRY
async def test_a_throttled_call_waits_a_growing_jittered_interval(
    drive: Driver, entry: Entry
) -> None:
    run = await drive(entry, [rate_limited()])

    assert isinstance(run.error, RateLimitError)
    assert len(run.calls) == 3
    assert len(run.slept) == 2, "every retry but the last waits first"
    assert 1.0 <= run.slept[0] <= 2.0
    assert 2.0 <= run.slept[1] <= 4.0
    assert run.throttled == 2
    assert run.retries == 2
    assert run.retry_announcements == [("retry", "INFO")] * 2
    assert run.logged == WAITED_THEN_GAVE_UP
    assert len(set(run.max_tokens)) == 1, "a throttle does not change the rung"
    assert set(run.thinking) == {"enabled"}


@EVERY_ENTRY
async def test_a_throttled_call_recovers_after_one_wait(drive: Driver, entry: Entry) -> None:
    run = await drive(entry, [rate_limited(), ok(entry)])

    assert run.error is None
    assert _answer(entry, run.result) == _expected(entry)
    assert len(run.calls) == 2
    assert len(run.slept) == 1
    assert run.throttled == 1
    assert run.retries == 1
    assert run.logged == [("failed", "WARNING"), ("waited", "WARNING")]


@EVERY_ENTRY
async def test_a_platform_cap_parks_the_task_without_waiting_or_retrying(
    drive: Driver, entry: Entry
) -> None:
    run = await drive(entry, [rate_limited(reset_in=7200)])

    assert isinstance(run.error, LLMRateLimitParkError)
    assert run.error.reason == "x_ratelimit_reset_header"
    assert run.error.resume_at == pytest.approx(time.time() + 7200, abs=5.0)
    assert len(run.calls) == 1
    assert run.slept == []
    assert run.throttled == 0
    assert run.retries == 0
    assert run.logged == [("park", "WARNING")]


@EVERY_ENTRY
async def test_a_provider_outage_waits_minutes_and_is_not_throttling(
    drive: Driver, entry: Entry
) -> None:
    run = await drive(entry, [overloaded()])

    assert isinstance(run.error, APIError)
    assert len(run.calls) == 3
    assert 15.0 <= run.slept[0] <= 30.0
    assert 30.0 <= run.slept[1] <= 60.0
    assert len(run.slept) == 2
    assert run.throttled == 0, "an overload must not shrink the next wave"
    assert run.retries == 2
    assert run.logged == WAITED_THEN_GAVE_UP


@EVERY_ENTRY
@pytest.mark.parametrize(
    ("failure", "error_type", "logged"),
    [
        (timed_out, LLMTimeoutError, [("terminal", "ERROR")]),
        (call_ceiling, LLMCallBudgetExceededError, [("terminal", "ERROR")]),
        (too_big, ContextWindow, [("terminal", "ERROR")]),
        (
            lambda: FreeModelEligibilityError("route is not free"),
            FreeModelEligibilityError,
            [],
        ),
    ],
    ids=[
        "timeout",
        "call-budget-ceiling",
        "context-window",
        "free-eligibility",
    ],
)
async def test_a_failure_the_same_request_cannot_survive_is_never_retried(
    drive: Driver,
    entry: Entry,
    failure: Callable[[], Exception],
    error_type: type[Exception],
    logged: list[tuple[str, str]],
) -> None:
    run = await drive(entry, [failure()])

    assert type(run.error) is error_type
    assert len(run.calls) == 1
    assert run.slept == []
    assert run.throttled == 0
    assert run.retries == 0
    assert run.logged == logged


@EVERY_ENTRY
async def test_an_ordinary_provider_error_is_retried_in_place_without_waiting(
    drive: Driver, entry: Entry
) -> None:
    run = await drive(entry, [RuntimeError("provider exploded")])

    assert isinstance(run.error, RuntimeError)
    assert len(run.calls) == 3
    assert run.slept == []
    assert run.retries == 2
    assert run.logged == [
        ("failed", "WARNING"),
        ("failed", "WARNING"),
        ("failed", "ERROR"),
    ]
    assert len(set(run.max_tokens)) == 1
    assert set(run.thinking) == {"enabled"}


@EVERY_ENTRY
async def test_budget_exhaustion_climbs_to_thinking_off_and_then_gives_up(
    drive: Driver, entry: Entry
) -> None:
    run = await drive(entry, [exhausted()])

    assert isinstance(run.error, LLMBudgetExhaustedError)
    assert len(run.calls) == 3, "the top rung is not resent"
    assert run.thinking == ["enabled", "enabled", "disabled"]
    first, raised, last = run.max_tokens
    assert raised > first
    assert last == raised
    assert run.slept == []
    assert run.retries == 2
    assert run.logged == [
        ("failed", "WARNING"),
        ("escalated", "WARNING"),
        ("failed", "WARNING"),
        ("escalated", "WARNING"),
        ("failed", "ERROR"),
    ]


@EVERY_ENTRY
async def test_a_thinking_only_attempt_skips_to_thinking_off_and_recovers(
    drive: Driver, entry: Entry
) -> None:
    run = await drive(entry, [thinking_only(), ok(entry)])

    assert run.error is None
    assert run.thinking == ["enabled", "disabled"]
    assert run.slept == []
    assert run.retries == 1
    assert run.logged == [("failed", "WARNING"), ("escalated", "WARNING")]


@TEXT_AND_JSON
async def test_standard_plans_can_revisit_rungs_within_their_attempt_budget(
    drive: Driver, entry: Entry
) -> None:
    entry = replace(entry, model=GATEWAY_MODEL)
    run = await drive(
        entry,
        [thinking_only(), reasoning_mandatory()] * 2 + [ok(entry)],
        max_attempts=5,
    )

    assert run.error is None
    assert run.result == _expected(entry)
    assert len(run.calls) == 5
    assert run.slept == []
    assert run.reasoning == [
        {"enabled": True, "effort": "medium"},
        {"enabled": False},
        {"enabled": True, "effort": "low"},
        {"enabled": False},
        {"enabled": True, "effort": "low"},
    ]


@TEXT_AND_JSON
async def test_a_mandatory_reasoning_refusal_is_answered_by_minimal_effort(
    drive: Driver, entry: Entry
) -> None:
    entry = replace(
        entry,
        model=GATEWAY_MODEL,
        options=LLMCallOptions(enable_thinking=False),
    )

    run = await drive(entry, [reasoning_mandatory(), ok(entry)])

    assert run.error is None
    assert run.reasoning == [
        {"enabled": False},
        {"enabled": True, "effort": "low"},
    ]
    assert run.max_tokens[1] > run.max_tokens[0]
    assert run.slept == [], "a 400 is a request problem, not a wait"
    assert run.logged == [("failed", "WARNING"), ("escalated", "WARNING")]


@TEXT_AND_JSON
async def test_a_repeated_mandatory_reasoning_refusal_exhausts_the_attempts(
    drive: Driver, entry: Entry
) -> None:
    entry = replace(
        entry,
        model=GATEWAY_MODEL,
        options=LLMCallOptions(enable_thinking=False),
    )

    run = await drive(entry, [reasoning_mandatory()])

    assert isinstance(run.error, BadRequestError)
    assert len(run.calls) == 3
    assert run.reasoning[1:] == [{"enabled": True, "effort": "low"}] * 2
    assert run.logged == [
        ("failed", "WARNING"),
        ("escalated", "WARNING"),
        ("failed", "WARNING"),
        ("failed", "ERROR"),
    ]


async def test_a_schema_failure_retries_at_once_with_feedback_on_the_same_rung(
    drive: Driver,
) -> None:
    run = await drive(JSON, [wrong_type(), ok(JSON)])

    assert run.result == {"a": 1}
    assert len(run.calls) == 2
    assert run.slept == []
    assert run.retries == 1
    first, second = run.prompts
    assert SCHEMA_FEEDBACK not in first
    assert second.startswith("a prompt")
    assert SCHEMA_FEEDBACK in second
    assert run.max_tokens[0] == run.max_tokens[1]
    assert run.thinking == ["enabled", "enabled"]
    assert run.logged == [("schema", "WARNING")]


async def test_a_schema_failure_on_every_attempt_raises_the_validation_error(
    drive: Driver,
) -> None:
    run = await drive(JSON, [wrong_type()])

    assert isinstance(run.error, ValidationError)
    assert "after 3 attempts" in run.error.message
    assert len(run.calls) == 3
    assert run.slept == []
    assert run.prompts[2].count(SCHEMA_FEEDBACK) == 1


async def test_a_schema_failure_holds_the_rung_an_escalation_reached(
    drive: Driver,
) -> None:
    run = await drive(JSON, [exhausted(), wrong_type(), ok(JSON)], max_attempts=4)

    assert run.result == {"a": 1}
    first, raised, held = run.max_tokens
    assert raised > first
    assert held == raised
    assert run.thinking == ["enabled", "enabled", "enabled"]


async def test_feedback_outlives_an_attempt_that_failed_before_judging(
    drive: Driver,
) -> None:
    run = await drive(JSON, [wrong_type(), RuntimeError("blip"), ok(JSON)], max_attempts=4)

    assert run.result == {"a": 1}
    assert SCHEMA_FEEDBACK in run.prompts[1]
    assert run.prompts[2] == run.prompts[1]


async def test_a_final_attempt_call_failure_is_not_a_parse_error(
    drive: Driver,
) -> None:
    run = await drive(JSON, [wrong_type(), RuntimeError("provider exploded")], max_attempts=2)

    assert isinstance(run.error, RuntimeError)
    assert len(run.calls) == 2


async def test_a_tool_turn_mandatory_reasoning_rung_sends_no_minimal_effort(
    drive: Driver,
) -> None:
    entry = replace(TOOLS, model=GATEWAY_MODEL)

    run = await drive(entry, [reasoning_mandatory(), ok(entry)])

    assert run.error is None
    assert len(run.calls) == 2
    # Tool recovery raises budget while retaining the same reasoning request.
    assert run.reasoning == [{"enabled": True, "effort": "medium"}] * 2
    assert run.max_tokens[1] > run.max_tokens[0]


@pytest.mark.parametrize("mandatory_first", [False, True])
async def test_alternating_reasoning_failures_exhaust_the_tool_attempt_budget(
    drive: Driver, mandatory_first: bool
) -> None:
    entry = replace(TOOLS, model=GATEWAY_MODEL)
    mandatory = reasoning_mandatory()
    pair = [mandatory, thinking_only()] if mandatory_first else [thinking_only(), mandatory]
    run = await drive(entry, pair * 10 + [ok(entry)])

    if mandatory_first:
        assert run.error is mandatory
    else:
        assert isinstance(run.error, LLMThinkingOnlyError)
    assert run.result is None, "the success sentinel must remain unreachable"
    assert len(run.calls) == 3
    enabled = {"enabled": True, "effort": "medium"}
    disabled = {"enabled": False}
    assert run.reasoning == (
        [enabled, enabled, disabled] if mandatory_first else [enabled, disabled, enabled]
    )
    assert run.max_tokens == [18000, 24000, 24000]
    assert all(c["messages"] == run.calls[0]["messages"] for c in run.calls)
    assert run.slept == []
    assert run.throttled == 0
    assert run.retries == 2
    assert run.logged[-1] == ("failed", "ERROR")


async def test_each_tool_turn_receives_its_own_three_attempt_budget(
    drive: Driver,
) -> None:
    asked_for_a_tool = make_completion(
        make_message(None, tool_calls=[make_tool_call("c1", "search", "{}")])
    )
    executed: list[str] = []

    async def executor(tc: Any) -> dict[str, Any]:
        executed.append(tc.id)
        return await echo_executor(tc)

    entry = replace(TOOLS, model=GATEWAY_MODEL, executor=executor)
    recovery = [exhausted(), exhausted()]
    run = await drive(entry, [*recovery, asked_for_a_tool, *recovery, ok(entry)])

    assert run.error is None
    assert run.result[0] == "fine"
    assert len(run.calls) == 6
    assert run.max_tokens == [18000, 24000, 24000] * 2
    assert run.reasoning[:3] == run.reasoning[3:]
    assert executed == ["c1"]
    for call in run.calls[3:]:
        assert [m["role"] for m in call["messages"]] == [
            "user",
            "assistant",
            "tool",
        ]
    assert run.retries == 4


@pytest.mark.parametrize("failure", [exhausted, rate_limited, overloaded])
async def test_a_tool_turn_retry_never_spans_tool_execution(
    drive: Driver,
    failure: Callable[[], Any],
) -> None:
    asked_for_a_tool = make_completion(
        make_message(None, tool_calls=[make_tool_call("c1", "search", "{}")])
    )
    executed: list[str] = []

    async def executor(tc: Any) -> dict[str, Any]:
        executed.append(tc.id)
        return await echo_executor(tc)

    entry = replace(TOOLS, executor=executor)

    run = await drive(entry, [asked_for_a_tool, failure(), ok(entry)])

    assert run.error is None
    assert len(run.calls) == 3
    assert executed == ["c1"], "the answerless turn must not rerun the tools"
    roles = [m["role"] for m in run.calls[2]["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert run.calls[1]["messages"] == run.calls[2]["messages"]
    assert run.max_tokens[1] == run.max_tokens[0]
    if failure is exhausted:
        assert run.max_tokens[2] > run.max_tokens[1]
    else:
        assert run.max_tokens[2] == run.max_tokens[1]
