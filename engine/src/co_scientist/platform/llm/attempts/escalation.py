import dataclasses
import enum
import logging

from co_scientist.core.constants import (
    BUDGET_ESCALATION_MAX_INCREMENT,
    BUDGET_ESCALATION_MAX_TOKENS,
    MINIMAL_REASONING_MAX_TOKENS,
)
from co_scientist.core.exceptions import (
    LLMBudgetExhaustedError,
    LLMContentFilteredError,
    LLMThinkingOnlyError,
)
from co_scientist.platform.llm.request.thinking import effective_thinking_enabled
from co_scientist.platform.llm.values import CompletionSpec

logger = logging.getLogger(__name__)


class BudgetEscalation(enum.Enum):
    """Repeated answerless requests spend the same budget again.
    Escalation is call-local and must terminate, not fund unbounded reasoning.
    """

    NONE = "none"
    RAISED_BUDGET = "raised_budget"
    NO_THINKING = "no_thinking"
    MINIMAL_REASONING_REQUIRED = "minimal_reasoning_required"


# Self-mapping rungs terminate even when an instruction-recovery attempt returns
# no answer.
_ESCALATION_LADDER: dict[BudgetEscalation, BudgetEscalation] = {
    BudgetEscalation.NONE: BudgetEscalation.RAISED_BUDGET,
    BudgetEscalation.RAISED_BUDGET: BudgetEscalation.NO_THINKING,
    BudgetEscalation.NO_THINKING: BudgetEscalation.NO_THINKING,
    BudgetEscalation.MINIMAL_REASONING_REQUIRED: (BudgetEscalation.MINIMAL_REASONING_REQUIRED),
}


_ANSWERLESS_ERRORS = (
    LLMBudgetExhaustedError,
    LLMThinkingOnlyError,
    LLMContentFilteredError,
)


# Match exact classes: APIError ancestry includes request failures that need
# escalation, not a wait.
_TRANSIENT_ERROR_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "APIError",
        "APIConnectionError",
        "InternalServerError",
        "ServiceUnavailableError",
    }
)

# Request-error text can quote an overload; its class takes precedence.
_NON_TRANSIENT_ERROR_TYPE_NAMES: frozenset[str] = frozenset({"BadRequestError"})

# Mid-stream provider failures carry no dedicated type; the response layer emits
# ValueError.
_TRANSIENT_MESSAGE_CUES: tuple[str, ...] = (
    "temporarily overloaded",
    "upstream error",
    "reported an error mid-stream",
)


def _is_transient_not_found(error: BaseException) -> bool:
    """OpenRouter 404s with previous_errors mean exhausted routes.
    Without that list, waiting cannot fix a missing model.
    """
    return "previous_errors" in str(error)


def is_transient_provider_error(error: BaseException | None) -> bool:
    """Outages need time; request and answerless failures need correction.
    Rate-limit parking takes precedence.
    """
    if error is None or isinstance(error, _ANSWERLESS_ERRORS):
        return False
    name = type(error).__name__
    if name == "NotFoundError":
        return _is_transient_not_found(error)
    if name in _TRANSIENT_ERROR_TYPE_NAMES:
        return True
    return _has_transient_message(error)


def _has_transient_message(error: BaseException) -> bool:
    """Exclude request-error classes first: their text can quote an upstream
    overload.
    """
    if type(error).__name__ in _NON_TRANSIENT_ERROR_TYPE_NAMES:
        return False
    text = str(error).lower()
    return any(cue in text for cue in _TRANSIENT_MESSAGE_CUES)


def _is_reasoning_mandatory_error(error: BaseException | None) -> bool:
    """LiteLLM gives provider 400s the same class, so the refusal text is the
    signal.
    """
    if error is None:
        return False
    text = str(error).lower()
    return "reasoning is mandatory" in text and "cannot be disabled" in text


# Require refusal wording as well as reasoning and cap fields; budget failures
# name those fields too.
_REASONING_FIELD_CUES: tuple[str, ...] = ("max_tokens", "max tokens")
_REJECTION_CUES: tuple[str, ...] = (
    "not supported",
    "unsupported",
    "invalid",
    "not allowed",
)


def _is_reasoning_cap_rejected(error: BaseException | None) -> bool:
    """An unprobed host may reject the explicit cap; the served tier-name
    shape is the fallback.
    """
    if error is None or isinstance(error, _ANSWERLESS_ERRORS):
        return False
    text = str(error).lower()
    return (
        "reasoning" in text
        and any(cue in text for cue in _REASONING_FIELD_CUES)
        and any(cue in text for cue in _REJECTION_CUES)
    )


def _is_reasoning_instruction_refused(error: BaseException | None) -> bool:
    return _is_reasoning_mandatory_error(error) or _is_reasoning_cap_rejected(error)


def _escalate_once(current: BudgetEscalation, target: BudgetEscalation) -> BudgetEscalation | None:
    """A repeated fixed recovery rung cannot answer the same failure again."""
    return None if current is target else target


def escalation_for_error(
    error: BaseException | None, current: BudgetEscalation
) -> BudgetEscalation | None:
    """Normal thinking-only stops did not want more room; length stops may.
    Provider aborts remain retries even after reasoning spend.
    """
    if _is_reasoning_instruction_refused(error):
        return _escalate_once(current, BudgetEscalation.MINIMAL_REASONING_REQUIRED)
    if isinstance(error, LLMThinkingOnlyError):
        return _escalate_once(current, BudgetEscalation.NO_THINKING)
    if not isinstance(error, LLMBudgetExhaustedError):
        return None
    escalated = _ESCALATION_LADDER[current]
    return None if escalated is current else escalated


def escalated_max_tokens(max_tokens: int, escalation: BudgetEscalation) -> int:
    """Raise even callers already above the floor, while bounding the extra
    allowance.
    """
    if escalation is BudgetEscalation.NONE:
        return max_tokens
    increment = min(max_tokens // 2, BUDGET_ESCALATION_MAX_INCREMENT)
    return max(max_tokens + increment, BUDGET_ESCALATION_MAX_TOKENS)


def _no_thinking_detail_text(model_name: str) -> str:
    """Models forbidding a disable receive capped reasoning; diagnostics must
    describe the wire.
    """
    return (
        f"reasoning capped at {MINIMAL_REASONING_MAX_TOKENS} tokens"
        if effective_thinking_enabled(model_name, False)
        else "thinking disabled"
    )


def log_escalation(
    error: BaseException | None,
    escalated: BudgetEscalation,
    model_name: str,
) -> None:
    """Only the retry decision knows what follows; the raising layer already
    records the error.
    """
    if escalated is BudgetEscalation.MINIMAL_REASONING_REQUIRED:
        logger.warning(
            "Provider rejected the requested reasoning configuration; retrying "
            "with reasoning enabled at minimal effort and a raised budget"
        )
        return
    detail = (
        _no_thinking_detail_text(model_name)
        if escalated is BudgetEscalation.NO_THINKING
        else "a raised token budget"
    )
    if isinstance(error, LLMThinkingOnlyError):
        logger.warning(
            "LLM finished thinking without answering; retrying with %s",
            detail,
        )
        return
    logger.warning(
        "LLM spent its whole token budget reasoning; retrying with %s",
        detail,
    )


def escalated_spec(spec: CompletionSpec, escalation: BudgetEscalation) -> CompletionSpec:
    if escalation is BudgetEscalation.NONE:
        return spec
    return dataclasses.replace(spec, max_tokens=escalated_max_tokens(spec.max_tokens, escalation))
