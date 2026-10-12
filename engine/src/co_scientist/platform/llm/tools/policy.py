"""Each turn rebills the growing transcript; bound tokens as well as turns."""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from co_scientist.platform.llm.tools.loop import ToolLoop

logger = logging.getLogger(__name__)


# Warn as a user turn: several providers ignore mid-conversation system
# messages.
_HANDOFF_FRACTION = 0.8

# Tiny loops would be warned before doing work worth wrapping up.
_MIN_ITERATIONS_FOR_HANDOFF = 4

# The loose default catches nonconvergence; measured callers need their own
# spend allowance.
DEFAULT_TOOL_LOOP_TOKEN_BUDGET = 300_000

# A local estimate avoids per-turn usage plumbing for a ceiling tolerant of
# small estimation errors.
_CHARS_PER_TOKEN = 4


def transcript_tokens(messages: list[dict[str, Any]]) -> int:
    chars = 0
    for message in messages:
        chars += len(str(message.get("content") or ""))
        calls = message.get("tool_calls")
        if calls:
            chars += len(str(calls))
    return chars // _CHARS_PER_TOKEN


def _handoff_iteration(max_iterations: int) -> int:
    if max_iterations < _MIN_ITERATIONS_FOR_HANDOFF:
        return -1
    return int(max_iterations * _HANDOFF_FRACTION)


def _handoff_spend(max_prompt_tokens: int) -> int:
    return int(max_prompt_tokens * _HANDOFF_FRACTION)


def _turns_remaining(
    iteration: int, spent: int, loop: "ToolLoop", messages: list[dict[str, Any]]
) -> int:
    """The wrap-up promise must reflect the binding spend ceiling, not just
    remaining turns.
    """
    by_turns = loop.max_iterations - iteration
    per_turn = max(transcript_tokens(messages), 1)
    by_spend = (loop.max_prompt_tokens - spent) // per_turn
    return max(1, min(by_turns, by_spend))


def _handoff_message(remaining: int) -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            f"You have {remaining} tool-calling turn(s) left before this "
            "task is stopped. Stop opening new lines of investigation. "
            "Use any remaining turns to finish what is in progress, then "
            "give your final answer, reporting what you established and "
            "what remains uncertain. A partial answer that says what is "
            "missing is far more useful than being cut off mid-step."
        ),
    }


def closing_message() -> dict[str, Any]:
    """Removing tools alone can yield plans for unavailable work; explicitly
    require the observed answer.
    """
    return {
        "role": "user",
        "content": (
            "Your tools are no longer available and this is your final"
            " turn. Do not describe what you would do next and do not"
            " propose further steps -- neither is possible now. Write"
            " your answer from what you have already established:"
            " what you did, the actual numbers you saw, what they imply,"
            " and what is still uncertain because you ran out of room."
        ),
    }
