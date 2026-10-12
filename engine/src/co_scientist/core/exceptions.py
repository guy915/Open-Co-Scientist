# Failure domains are independent, so subclasses stay flat.
class CoScientistError(Exception):
    """Base class for all errors raised by the co-scientist engine."""


class FreeModelEligibilityError(CoScientistError, RuntimeError):
    """A request lacks zero-cost eligibility; do not send or retry it."""


class ConfigError(CoScientistError):
    """The tool registry or configuration is not initialized or invalid."""


class GenerationError(CoScientistError):
    """Hypothesis generation or debate failed to produce a result."""


class ResponseParseError(CoScientistError):
    """An LLM response could not be parsed or repaired into expected JSON."""


# A stalled provider will not answer an identical retry sooner.
class LLMTimeoutError(CoScientistError):
    """Only exact zero-cost admission without caller credentials permits
    bounded automatic recovery. Other timeouts require explicit recovery.
    """

    def __init__(self, message: str, *, zero_cost_admitted: bool = False) -> None:
        super().__init__(message)
        self.zero_cost_admitted = zero_cost_admitted


# Budget exhaustion needs a changed request; ValueError preserves existing
# empty-response callers.
class LLMBudgetExhaustedError(CoScientistError, ValueError):
    """An LLM spent its whole token budget reasoning and answered nothing."""


# Normal reasoning-only completion needs thinking disabled, not more room;
# preserve ValueError callers.
class LLMThinkingOnlyError(CoScientistError, ValueError):
    """An LLM finished its chain of thought and wrote no answer at all."""


# A provider filter verdict keeps the request unchanged; preserve ValueError
# callers.
class LLMContentFilteredError(CoScientistError, ValueError):
    """A provider content filter withheld the answer."""


# Enforce ceilings inside tasks; not ValueError, which workers treat as
# transient.
class LLMCallBudgetExceededError(CoScientistError):
    """An exhausted call ceiling must abort rather than spend more on
    retries.
    """

    def __init__(self, count: int, ceiling: int) -> None:
        self.count = count
        self.ceiling = ceiling
        super().__init__(
            f"LLM-call ceiling exceeded: {count} provider requests against "
            f"a budget of {ceiling}; aborting the run"
        )


# Long platform caps outlast bounded backoff; not ValueError, which would spend
# durable retries.
class ProviderAdmissionError(LLMCallBudgetExceededError):
    """Durable service admission failed; never retry a rejected dispatch."""

    def __init__(self, reason: str) -> None:
        self.count = 0
        self.ceiling = 0
        CoScientistError.__init__(self, reason)


class ProviderCreditExhaustedError(ProviderAdmissionError):
    """The provider rejected dispatch until its account is replenished."""


class LLMRateLimitParkError(CoScientistError):
    """Long-lived platform caps require parking rather than consuming
    transient retries.
    """

    def __init__(self, resume_at: float, reason: str) -> None:
        self.resume_at = resume_at
        self.reason = reason
        super().__init__(
            f"platform rate limit hit ({reason}); resume at {resume_at:.0f} (epoch seconds)"
        )


# Re-raise TASK_CONTROL_FLOW_ERRORS before broad fallback handlers so the worker
# can park or terminate.
TASK_CONTROL_FLOW_ERRORS: tuple[type[CoScientistError], ...] = (
    LLMCallBudgetExceededError,
    LLMRateLimitParkError,
)


# Tool streams need their own deadline; LLM deadlines do not bound MCP awaits.
class MCPToolTimeoutError(CoScientistError):
    """An MCP tool call exceeded its wall-clock budget without responding."""


# Provider payload echoes can flood the single-writer log store and bury the
# diagnosis.
_MAX_LOGGED_ERROR_CHARS = 400


def short_error_text(error: BaseException) -> str:
    """Keep the diagnostic head: provider errors append large payloads after
    the failure.
    """
    text = str(error).strip()
    dropped = len(text) - _MAX_LOGGED_ERROR_CHARS
    if dropped <= 0:
        return text
    return f"{text[:_MAX_LOGGED_ERROR_CHARS]}... (+{dropped} more chars)"


class ContinuationAdmissionError(CoScientistError):
    def __init__(self, reason: str, *, capacity: bool = False) -> None:
        self.capacity = capacity
        super().__init__(reason)


class StorageAdmissionError(CoScientistError):
    pass
