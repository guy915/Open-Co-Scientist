from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from co_scientist.core.exceptions import LLMContentFilteredError


@dataclass
class ReasoningRetry:
    answered: bool = False
    reasoned: bool = False
    tool_requested: bool = False

    def observe(
        self, *, prose: str = "", reasoned: bool = False, tool_requested: bool = False
    ) -> None:
        self.answered = self.answered or bool(prose.strip())
        self.reasoned = self.reasoned or reasoned
        self.tool_requested = self.tool_requested or tool_requested

    def attempts(self) -> Iterator[bool]:
        # The caller must finish its first attempt cleanly before advancing.
        yield True
        if self.reasoned and not self.answered and not self.tool_requested:
            yield False


def check_text_response(response: Any, *, allow_tools: bool = False) -> None:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return
    choice = choices[0]
    message = getattr(choice, "message", None) or getattr(choice, "delta", None)
    if getattr(choice, "finish_reason", None) in {"content_filter", "refusal"} or getattr(
        message, "refusal", None
    ):
        raise LLMContentFilteredError("The provider refused the response.")
    if not allow_tools and getattr(message, "tool_calls", None):
        raise ValueError("Unexpected tool request during text generation.")
