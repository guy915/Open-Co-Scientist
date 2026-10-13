"""Parse failures must not change token allowance; escalation is a separate
policy.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Generic, TypeVar, cast, overload

from litellm.exceptions import ContextWindowExceededError

from co_scientist.core.backoff import jittered_backoff_seconds
from co_scientist.core.exceptions import (
    FreeModelEligibilityError,
    LLMCallBudgetExceededError,
    LLMRateLimitParkError,
    LLMTimeoutError,
    short_error_text,
)
from co_scientist.platform.llm.attempts.escalation import (
    BudgetEscalation,
    escalation_for_error,
    is_transient_provider_error,
    log_escalation,
)
from co_scientist.platform.llm.request.thinking import failure_context_text
from co_scientist.platform.llm.telemetry import attempt_span, record_retry, record_retry_reason

logger = logging.getLogger(__name__)


R = TypeVar("R")
T = TypeVar("T")


@dataclass(frozen=True)
class Attempt:
    """Rejection feedback survives an intervening call failure so the next
    attempt still learns.
    """

    number: int
    is_final: bool
    rung: BudgetEscalation = BudgetEscalation.NONE
    feedback: str | None = None


@dataclass(frozen=True)
class Accepted(Generic[T]):
    value: T


@dataclass(frozen=True)
class Rejected:
    error: Exception
    response_text: str | None = None
    feedback: str | None = None


@dataclass(frozen=True)
class Judge(Generic[R, T]):
    """A final call failure propagates directly; exhaustion handles rejected
    responses only.
    """

    verdict: Callable[[R, Attempt], Accepted[T] | Rejected]
    exhausted: Callable[[Rejected], T]


@dataclass(frozen=True)
class AttemptPlan:
    model_name: str
    max_attempts: int


def is_rate_limited(error: Exception) -> bool:
    """Provider SDKs can surface throttling as generic errors, so text
    complements the type.
    """
    if type(error).__name__ == "RateLimitError":
        return True
    text = str(error).lower()
    return "rate limit" in text or "ratelimit" in text


def is_credential_rejected(error: Exception) -> bool:
    """Classify by name rather than importing the provider SDK, which the
    layering keeps inside this gateway.
    """
    if type(error).__name__ in {"AuthenticationError", "PermissionDeniedError"}:
        return True
    text = str(error).lower()
    return "invalid api key" in text or "incorrect api key" in text


# Short caps fit ordinary backoff; daily caps require durable parking.
_PLATFORM_PARK_THRESHOLD_SECONDS = 90.0

# Without headers, a burst estimate stays below the durable park threshold.
_PER_MINUTE_DEFAULT_SECONDS = 60.0


def _next_utc_midnight_epoch(now: float) -> float:
    current = datetime.fromtimestamp(now, tz=timezone.utc)
    tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _platform_reset_from_headers(error: Exception, now: float) -> tuple[float, str] | None:
    """LiteLLM exposes upstream headers through response, not a headers
    shortcut.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    retry_after = _parse_float(headers.get("retry-after"))
    if retry_after is not None:
        return now + retry_after, "retry_after_header"
    reset_ms = _parse_float(headers.get("x-ratelimit-reset"))
    if reset_ms is not None:
        return reset_ms / 1000.0, "x_ratelimit_reset_header"
    return None


def _platform_reset_from_message(error: Exception, now: float) -> tuple[float, str] | None:
    """Without headers, daily caps use UTC reset; short burst waits stay
    below the park threshold.
    """
    # OpenRouter hyphenates free-models-per-day; accept plain-English wording
    # too.
    text = str(error).lower()
    if "per day" in text or "per-day" in text:
        return _next_utc_midnight_epoch(now), "message_per_day"
    if "per minute" in text or "per-minute" in text or "free-models" in text:
        return now + _PER_MINUTE_DEFAULT_SECONDS, "message_per_minute"
    return None


def platform_rate_limit_park(
    error: Exception,
) -> LLMRateLimitParkError | None:
    """Trust exact reset headers before message estimates; absorb short waits
    in ordinary backoff.
    """
    now = time.time()
    resolved = _platform_reset_from_headers(error, now) or _platform_reset_from_message(error, now)
    if resolved is None:
        return None
    resume_at, reason = resolved
    if resume_at - now <= _PLATFORM_PARK_THRESHOLD_SECONDS:
        return None
    return LLMRateLimitParkError(resume_at=resume_at, reason=reason)


_RATE_LIMIT_BACKOFF_BASE_SECONDS: Final[float] = 2.0

# Awaited backoff permits lease heartbeats; per-call timeouts exclude these
# gaps.
# The wait ceiling prevents larger attempt plans from doubling into indefinite
# stalls.
_PROVIDER_OUTAGE_BACKOFF_BASE_SECONDS: Final[float] = 30.0
_PROVIDER_OUTAGE_BACKOFF_MAX_SECONDS: Final[float] = 240.0


def _rate_limit_backoff_seconds(attempt: int) -> float:
    """The attempt budget bounds this uncapped wait; jitter avoids
    synchronized retry bursts.
    """
    return jittered_backoff_seconds(attempt, base_seconds=_RATE_LIMIT_BACKOFF_BASE_SECONDS)


def provider_outage_backoff_seconds(attempt: int) -> float:
    """Outages clear on the provider timetable, unlike bursts. A burst-sized
    schedule can spend every attempt before recovery.
    """
    return jittered_backoff_seconds(
        attempt,
        base_seconds=_PROVIDER_OUTAGE_BACKOFF_BASE_SECONDS,
        max_seconds=_PROVIDER_OUTAGE_BACKOFF_MAX_SECONDS,
    )


# A plain process counter crosses worker event loops safely; throttling shrinks
# later fan-out.
_rate_limited_attempts = 0


def rate_limited_attempt_count() -> int:
    return _rate_limited_attempts


# Identical retries cannot fix timeouts, exhausted call ceilings or oversized
# transcripts.
_NEVER_RETRIED: tuple[tuple[type[Exception], str], ...] = (
    (
        LLMCallBudgetExceededError,
        "LLM-call ceiling exceeded on attempt %s; not retrying",
    ),
    (LLMTimeoutError, "LLM call timed out on attempt %s; not retrying"),
    (
        ContextWindowExceededError,
        "Prompt exceeded the model's context window on attempt %s;"
        " not retrying, since the same prompt cannot fit on a retry",
    ),
)


def _raise_if_platform_cap(error: Exception, attempt: Attempt) -> None:
    """A daily cap outlives every in-call attempt; park durable work instead
    of spending retries.
    """
    if not is_rate_limited(error):
        return
    park = platform_rate_limit_park(error)
    if park is None:
        return
    logger.warning(
        "Platform rate limit cap hit on attempt %s (%s); parking "
        "until %.0f (epoch seconds) instead of retrying",
        attempt.number,
        park.reason,
        park.resume_at,
    )
    raise park


def _raise_if_unretryable(error: Exception, attempt: Attempt) -> None:
    for kind, line in _NEVER_RETRIED:
        if isinstance(error, kind):
            logger.error(line, attempt.number)
            raise error
    if isinstance(error, FreeModelEligibilityError):
        raise error
    _raise_if_platform_cap(error, attempt)


def _log_failure(error: Exception, attempt: Attempt) -> None:
    """Recovered attempts are warnings; only this boundary knows when failure
    is terminal.
    """
    log = logger.error if attempt.is_final else logger.warning
    log(
        "LLM call failed on attempt %s%s: %s",
        attempt.number,
        failure_context_text(error),
        short_error_text(error),
    )


async def _wait_before_retry(error: Exception, attempt: Attempt) -> None:
    """Only throttles affect fan-out sizing; an outage does not mean the
    provider is rationing us.
    """
    global _rate_limited_attempts
    if is_rate_limited(error):
        _rate_limited_attempts += 1
        reason = "Rate limited"
        delay = _rate_limit_backoff_seconds(attempt.number)
    elif is_transient_provider_error(error):
        reason = "Transient provider failure"
        delay = provider_outage_backoff_seconds(attempt.number)
    else:
        return
    logger.warning(
        "%s on attempt %s; waiting %.1fs before retrying",
        reason,
        attempt.number,
        delay,
    )
    await asyncio.sleep(delay)


async def _answer_call_failure(error: Exception, attempt: Attempt) -> None:
    _raise_if_unretryable(error, attempt)
    _log_failure(error, attempt)
    if attempt.is_final:
        raise error
    await _wait_before_retry(error, attempt)


def _next_rung(
    error: Exception, current: BudgetEscalation, model_name: str
) -> BudgetEscalation | None:
    """Schema and parse errors need correction, not more tokens."""
    escalated = escalation_for_error(error, current)
    if escalated is not None:
        log_escalation(error, escalated, model_name)
    return escalated


def _numbers(plan: AttemptPlan) -> range:
    return range(1, plan.max_attempts + 1)


class _AttemptRun(Generic[R, T]):
    def __init__(
        self,
        make_attempt: Callable[[Attempt], Awaitable[R]],
        plan: AttemptPlan,
        judge: Judge[R, T] | None,
    ) -> None:
        self._make_attempt = make_attempt
        self._plan = plan
        self._judge = judge
        self._rung = BudgetEscalation.NONE
        self._feedback: str | None = None
        self._response_text: str | None = None
        self._last: Rejected | None = None

    async def run(self) -> T:
        for number in _numbers(self._plan):
            self._announce(number)
            attempt = Attempt(
                number,
                number == self._plan.max_attempts,
                self._rung,
                self._feedback,
            )
            verdict = await self._attempt_once(attempt)
            if isinstance(verdict, Accepted):
                return verdict.value
            self._absorb(verdict)
        return self._exhausted()

    def _announce(self, number: int) -> None:
        if number == 1:
            return
        logger.info(
            "retrying llm call (attempt %s/%s)",
            number,
            self._plan.max_attempts,
        )
        record_retry(self._plan.model_name)

    async def _attempt_once(self, attempt: Attempt) -> Accepted[T] | Rejected:
        with attempt_span(attempt.number, attempt.rung.value):
            verdict = await self._judged_attempt(attempt)
            if isinstance(verdict, Rejected):
                record_retry_reason(verdict.error)
            return verdict

    async def _judged_attempt(self, attempt: Attempt) -> Accepted[T] | Rejected:
        try:
            response = await self._make_attempt(attempt)
            return self._judge_response(response, attempt)
        except Exception as error:
            await _answer_call_failure(error, attempt)
            return Rejected(error)

    def _judge_response(self, response: R, attempt: Attempt) -> Accepted[T] | Rejected:
        if self._judge is None:
            return Accepted(cast(T, response))
        return self._judge.verdict(response, attempt)

    def _absorb(self, rejected: Rejected) -> None:
        self._climb(rejected.error)
        if rejected.response_text is not None:
            self._response_text = rejected.response_text
        if rejected.feedback is not None:
            self._feedback = rejected.feedback
            logger.debug("added validation feedback to retry prompt")
        self._last = rejected

    def _climb(self, error: Exception) -> None:
        rung = _next_rung(error, self._rung, self._plan.model_name)
        if rung is not None:
            self._rung = rung

    def _exhausted(self) -> T:
        if self._judge is None:
            raise AssertionError("unreachable: a final attempt's failure always re-raises")
        # A zero-attempt plan has no rejection to give the judge.
        last = self._last or Rejected(ValueError("no attempt was made"))
        return self._judge.exhausted(Rejected(last.error, self._response_text))


@overload
async def run_attempts(make_attempt: Callable[[Attempt], Awaitable[R]], plan: AttemptPlan) -> R: ...


@overload
async def run_attempts(
    make_attempt: Callable[[Attempt], Awaitable[R]],
    plan: AttemptPlan,
    judge: Judge[R, T],
) -> T: ...


async def run_attempts(
    make_attempt: Callable[[Attempt], Awaitable[Any]],
    plan: AttemptPlan,
    judge: Judge[Any, Any] | None = None,
) -> Any:
    """Attempts must not retry or log failures themselves: this boundary owns
    both decisions.
    """
    return await _AttemptRun(make_attempt, plan, judge).run()
