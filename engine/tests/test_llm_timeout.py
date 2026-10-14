from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from litellm.exceptions import (
    APIConnectionError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
    ServiceUnavailableError,
)
from litellm.exceptions import Timeout as LiteLLMTimeout

from co_scientist.core import backoff
from co_scientist.core.exceptions import (
    LLMRateLimitParkError,
    LLMThinkingOnlyError,
    LLMTimeoutError,
)
from co_scientist.platform.llm import (
    CompletionSpec,
    call_llm,
    scoped_telemetry,
)
from co_scientist.platform.llm.attempts import retry
from co_scientist.platform.llm.attempts.retry import platform_rate_limit_park
from co_scientist.platform.llm.request import completion
from tests._llm_fake import (
    TEXT,
    Driver,
    install_fake_backend,
    ok,
    overloaded,
    rate_limited,
)
from tests._llm_fake import drive as drive

__all__ = ["drive"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, completion.DEFAULT_LLM_TIMEOUT_SECONDS),
        ("12.5", 12.5),
        ("0", None),
        ("-1", None),
        ("soon", completion.DEFAULT_LLM_TIMEOUT_SECONDS),
    ],
)
def test_the_timeout_comes_from_the_environment_and_zero_disables_it(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: float | None
) -> None:
    if value is None:
        monkeypatch.delenv(completion.LLM_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(completion.LLM_TIMEOUT_ENV, value)
    assert completion.llm_timeout_seconds() == expected


@pytest.mark.parametrize(
    ("value", "sent"), [("42", 42.0), ("0", None)], ids=["bounded", "disabled"]
)
async def test_the_provider_is_asked_to_stop_at_the_timeout(
    monkeypatch: pytest.MonkeyPatch, value: str, sent: float | None
) -> None:
    monkeypatch.setenv(completion.LLM_TIMEOUT_ENV, value)

    async def answer(**_kwargs: Any) -> Any:
        return ok(TEXT)

    backend = install_fake_backend(monkeypatch, answer)

    await call_llm("prompt", CompletionSpec("deepseek/deepseek-v4-flash"))

    assert backend.requests[0].get("timeout") == sent


async def test_a_hung_provider_is_cut_off_and_recorded_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(completion.LLM_TIMEOUT_ENV, "0.01")
    monkeypatch.setattr(completion, "_TIMEOUT_GRACE_SECONDS", 0.0)

    async def never_answers(**_kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    backend = install_fake_backend(monkeypatch, never_answers)
    model = "deepseek/deepseek-v4-pro"

    with (
        scoped_telemetry("test_phase") as telemetry,
        pytest.raises(LLMTimeoutError, match=model),
    ):
        await call_llm("prompt", CompletionSpec(model))

    assert len(backend.requests) == 1, "a timeout is never replayed"
    entry = telemetry.snapshot()[f"test_phase::{model}"]
    assert entry["errors"] == {"LLMTimeoutError": 1}
    assert entry["latency_seconds"] > 0


@pytest.mark.parametrize(
    ("api_key", "zero_cost_admitted"),
    [(None, True), ("byok-key", False)],
    ids=["house-key", "byok"],
)
async def test_a_provider_timeout_says_whether_a_zero_cost_request_went_out(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str | None,
    zero_cost_admitted: bool,
) -> None:
    model = "openrouter/nex-agi/nex-n2.5-pro:free"

    async def accepted_then_lost(**_kwargs: Any) -> Any:
        raise LiteLLMTimeout(
            message="read timed out after provider accepted request",
            model=model,
            llm_provider="openrouter",
        )

    backend = install_fake_backend(monkeypatch, accepted_then_lost)

    with pytest.raises(LLMTimeoutError) as excinfo:
        await call_llm("prompt", CompletionSpec(model, api_key=api_key))

    assert len(backend.requests) == 1, "an ambiguous call must not replay"
    assert excinfo.value.zero_cost_admitted is zero_cost_admitted
    if zero_cost_admitted:
        assert backend.requests[0]["extra_body"]["provider"]["max_price"] == {
            "prompt": 0,
            "completion": 0,
            "request": 0,
        }


def _rate_limit_error(message: str, *, headers: dict[str, str] | None = None) -> RateLimitError:
    response = None
    if headers is not None:
        response = httpx.Response(
            status_code=429,
            headers=headers,
            request=httpx.Request("POST", "https://openrouter.ai/api/v1"),
        )
    return RateLimitError(
        message=message,
        llm_provider="openrouter",
        model="test-model",
        response=response,
    )


_THREE_HOURS = 3 * 3600

# A real clock near UTC midnight puts the per-day reset seconds away, inside
# the ordinary wait, so the cases run at a fixed noon (2026-09-21 12:00 UTC).
_NOON_UTC = 1789992000


@pytest.mark.parametrize(
    ("make_error", "reason"),
    [
        (
            lambda now: _rate_limit_error(
                "rate limited",
                headers={"x-ratelimit-reset": str(int((now + _THREE_HOURS) * 1000))},
            ),
            "x_ratelimit_reset_header",
        ),
        (
            lambda now: _rate_limit_error("free-models-per-day rate limit exceeded"),
            "message_per_day",
        ),
        (lambda now: _rate_limit_error("rate-limited upstream, provider_code=x"), None),
        (lambda now: _rate_limit_error("limited", headers={"retry-after": "20"}), None),
        (lambda now: _rate_limit_error("free-models-per-minute rate limit exceeded"), None),
    ],
    ids=[
        "reset-header",
        "per-day",
        "upstream",
        "short-retry-after",
        "per-minute",
    ],
)
def test_only_a_platform_cap_that_outlasts_a_wait_parks_the_task(
    make_error: Callable[[float], RateLimitError],
    reason: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.platform.llm.attempts import retry

    monkeypatch.setattr(retry, "time", SimpleNamespace(time=lambda: float(_NOON_UTC)))
    park = platform_rate_limit_park(make_error(_NOON_UTC))

    if reason is None:
        assert park is None
        return
    assert isinstance(park, LLMRateLimitParkError)
    assert park.reason == reason
    assert 0 < park.resume_at - _NOON_UTC <= 86400


_MID_STREAM = (
    "LLM provider reported an error mid-stream and wrote no answer. "
    "Model: openrouter/nvidia/nemotron-3-super-120b-a12b:free "
    "(finish_reason=error, completion_tokens=0)"
)
_ROUTES_EXHAUSTED = (
    "litellm.NotFoundError: OpenrouterException - "
    '{"error":{"message":"Provider returned error","code":404,'
    '"metadata":{"raw":"","provider_name":"Nvidia","is_byok":false,'
    '"previous_errors":[{"code":429,"message":"Resource exhausted",'
    '"provider_name":"Google AI Studio"}]}}}'
)


def _provider_error(factory: Callable[..., Exception]) -> Exception:
    return factory(message="having a moment", llm_provider="openrouter", model="m")


def _not_found(message: str) -> NotFoundError:
    return NotFoundError(message=message, model="m", llm_provider="openrouter")


def _bad_request(message: str) -> BadRequestError:
    return BadRequestError(message=message, model="m", llm_provider="openrouter")


_THROTTLE = (1.0, 2.0)
_OUTAGE = (15.0, 30.0)


@pytest.mark.parametrize(
    ("error", "wait"),
    [
        (rate_limited(), _THROTTLE),
        (overloaded(), _OUTAGE),
        (_provider_error(InternalServerError), _OUTAGE),
        (_provider_error(ServiceUnavailableError), _OUTAGE),
        (_provider_error(APIConnectionError), _OUTAGE),
        (ValueError(_MID_STREAM), _OUTAGE),
        (_not_found(_ROUTES_EXHAUSTED), _OUTAGE),
        (ValueError("failed schema validation: 'title' is required"), None),
        (LLMThinkingOnlyError("wrote no answer. Model: m"), None),
        (_bad_request("Reasoning is mandatory and cannot be disabled."), None),
        (_bad_request("Upstream error from Nvidia: invalid request"), None),
        (_not_found("model 'no-such-model' not found"), None),
    ],
    ids=[
        "throttle",
        "overloaded",
        "internal-server-error",
        "service-unavailable",
        "connection-error",
        "mid-stream-failure",
        "routes-exhausted",
        "schema-failure",
        "thinking-only",
        "bad-request",
        "bad-request-quoting-overload",
        "bare-not-found",
    ],
)
async def test_a_failed_attempt_waits_by_kind_of_failure_not_by_its_wording(
    drive: Driver, error: Exception, wait: tuple[float, float] | None
) -> None:
    before = retry.rate_limited_attempt_count()

    run = await drive(TEXT, [error, ok(TEXT)], max_attempts=2)

    assert run.error is None
    if wait is None:
        assert run.slept == []
    else:
        assert wait[0] <= run.slept[0] <= wait[1]
    throttled = isinstance(error, RateLimitError)
    assert retry.rate_limited_attempt_count() - before == int(throttled), (
        "only throttling shrinks the next fan-out wave"
    )


def test_backoff_waits_are_jittered_within_a_doubling_ceiling() -> None:
    for attempt in range(1, 6):
        ceiling = 2.0 * 2 ** (attempt - 1)
        waits = {backoff.jittered_backoff_seconds(attempt, 2.0) for _ in range(40)}
        assert len(waits) > 1, "identical waits re-synchronize the burst"
        assert all(ceiling / 2 <= wait <= ceiling for wait in waits)
    assert backoff.jittered_backoff_seconds(9, 0.5, 8.0) <= 8.0


def test_an_outage_wait_grows_and_is_capped() -> None:
    wait = retry.provider_outage_backoff_seconds
    assert min(wait(4) for _ in range(50)) > max(wait(1) for _ in range(50))
    assert wait(10) <= retry._PROVIDER_OUTAGE_BACKOFF_MAX_SECONDS
