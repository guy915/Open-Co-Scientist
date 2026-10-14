from __future__ import annotations

import logging
from typing import Any

import co_scientist.platform.llm.offline_guard as offline_guard
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.core.json_schema import obj
from co_scientist.core.prompt_layout import render_cacheable_prompt
from co_scientist.platform.llm import CompletionSpec, LLMCallOptions, call_llm_json

logger = logging.getLogger(__name__)

# Question count stays bounded even when model output ignores instructions.
MAX_QUESTIONS = 3

# Options need readable labels while remaining within token bounds.
MIN_OPTIONS = 2
MAX_OPTIONS = 6


def _text(raw: Any) -> str:
    """Objects and numbers are not valid human-readable question labels."""
    return raw.strip() if isinstance(raw, str) else ""


def _normalized_option(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    label = _text(raw.get("label"))
    if not label:
        return None
    return {"label": label, "description": _text(raw.get("description"))}


def _normalized_options(raw: Any) -> list[dict[str, str]]:
    """Oversized option lists are truncated rather than discarded,
    preserving usable choices.
    """
    if not isinstance(raw, list):
        return []
    options = [option for option in map(_normalized_option, raw) if option]
    return options[:MAX_OPTIONS]


def _normalized_question(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    question = _text(raw.get("question"))
    options = _normalized_options(raw.get("options"))
    if not question or len(options) < MIN_OPTIONS:
        return None
    return {
        "header": _text(raw.get("header")),
        "question": question,
        "multi_select": bool(raw.get("multi_select")),
        "options": options,
    }


def normalized_questions(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning("Interview questions block is not a list; dropping it")
        return []
    questions = [q for q in map(_normalized_question, raw) if q]
    if raw and not questions:
        # Invalid options are logged because otherwise lost prose questions are
        # invisible to operators.
        logger.warning("Interview turn offered %d question(s), none usable", len(raw))
    return questions[:MAX_QUESTIONS]


# Repair recovers an existing question; it must never invent one.
_QUESTION_SCHEMA = obj(
    {
        "header": {"type": "string"},
        "question": {"type": "string"},
        "multi_select": {"type": "boolean"},
        "options": {
            "type": "array",
            "items": obj({"label": {"type": "string"}, "description": {"type": "string"}}),
        },
    }
)

# Reasoning headroom must not consume the answer budget.
_MAX_TOKENS = 1200

_PROMPT = """\
A research-goal interview turn has just been written to a scientist. Read \
it and return the question it ends on as clickable answers.

- ``question`` is that question, in full, in the turn's own words.
- ``header`` is a two-or-three word label for what is being chosen.
- ``options`` is {min} to {max} answers a scientist could plausibly click, \
each a short ``label`` and a one-line ``description`` of what choosing it \
would mean for the work. Where the answer space is open, enumerate the \
directions the answer could take rather than guessing at exact values.
- ``multi_select`` is true when several answers can hold at once and false \
when they are alternatives.

If the turn does not ask the scientist anything, return an empty \
``question`` and no options. Never invent a question the turn did not ask.

The turn:
---
{message}
---
"""


def _repair_prompt(message: str) -> str:
    instructions = _PROMPT.split("\nThe turn:\n", 1)[0].format(min=MIN_OPTIONS, max=MAX_OPTIONS)
    return render_cacheable_prompt(
        instructions,
        "",
        "The turn:\n---\n" + message.strip() + "\n---",
        "Recover the existing question and its answers. Never invent a question.",
    )


async def repair_questions(message: str) -> list[dict[str, Any]]:
    if not message.strip() or not offline_guard.remote_chat_allowed():
        return []
    model, api_key = byok_scope.byok_model_and_key(settings.effective_chat_model)
    spec = CompletionSpec(
        role="question_repair",
        model_name=model,
        max_tokens=_MAX_TOKENS,
        temperature=0,
        json_schema=_QUESTION_SCHEMA,
        api_key=api_key,
    )
    try:
        result = await call_llm_json(
            _repair_prompt(message),
            spec,
            max_attempts=2,
            options=LLMCallOptions(prompt_name="interview_question_repair", enable_thinking=False),
        )
    except Exception:
        logger.warning("Interview question repair failed", exc_info=True)
        return []
    return normalized_questions([result])
