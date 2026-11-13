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

# Options need readable labels while remaining within token bounds; more than
# four choices (plus the free-text answer) is too many to scan.
MIN_OPTIONS = 2
MAX_OPTIONS = 4


def _text(raw: Any) -> str:
    """Objects and numbers are not valid human-readable question labels."""
    return raw.strip() if isinstance(raw, str) else ""


def _normalized_option(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    label = _text(raw.get("label"))
    if not label:
        return None
    return {
        "label": label,
        "description": _text(raw.get("description")),
        "recommended": raw.get("recommended") is True,
    }


def _with_recommendation(options: list[dict[str, Any]], multi_select: bool) -> list[dict[str, Any]]:
    """Every question recommends an answer: exactly one for a single-select
    question, one or more for a multi-select one. A model that names none
    gets its first option, which the prompt asks it to rank first anyway.
    """
    flagged = [index for index, option in enumerate(options) if option["recommended"]]
    keep = set(flagged if multi_select else flagged[:1]) or {0}
    return [{**option, "recommended": index in keep} for index, option in enumerate(options)]


def _normalized_options(raw: Any) -> list[dict[str, Any]]:
    """Oversized option lists are truncated rather than discarded,
    preserving usable choices; recommended options are kept first.
    """
    if not isinstance(raw, list):
        return []
    options = [option for option in map(_normalized_option, raw) if option]
    if len(options) > MAX_OPTIONS:
        recommended = [option for option in options if option["recommended"]]
        others = [option for option in options if not option["recommended"]]
        kept = (recommended + others)[:MAX_OPTIONS]
        options = [option for option in options if option in kept]
    return options


def _normalized_question(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    question = _text(raw.get("question"))
    options = _normalized_options(raw.get("options"))
    if not question or len(options) < MIN_OPTIONS:
        return None
    multi_select = bool(raw.get("multi_select"))
    return {
        "header": _text(raw.get("header")),
        "question": question,
        "multi_select": multi_select,
        "options": _with_recommendation(options, multi_select),
    }


def normalized_questions(raw: Any, *, warn: bool = True) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        if warn:
            logger.warning("Interview questions block is not a list; dropping it")
        return []
    questions = [q for q in map(_normalized_question, raw) if q]
    # Repair answers "the turn asks nothing" with an empty question; that is
    # not an offered question and must not raise a warning.
    offered = [q for q in raw if not _is_empty_question(q)]
    if warn and offered and not questions:
        # Invalid options are logged because otherwise lost prose questions are
        # invisible to operators.
        logger.warning(
            "Interview turn offered %d question(s), none usable",
            len(offered),
            extra={"diagnostic_event": "interview.question_unusable"},
        )
    return questions[:MAX_QUESTIONS]


def _is_empty_question(raw: Any) -> bool:
    return isinstance(raw, dict) and not _text(raw.get("question")) and not raw.get("options")


# Repair recovers an existing question; it must never invent one.
QUESTION_SCHEMA = obj(
    {
        "header": {"type": "string"},
        "question": {"type": "string"},
        "multi_select": {"type": "boolean"},
        "options": {
            "type": "array",
            "items": obj(
                {
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "recommended": {"type": "boolean"},
                }
            ),
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
would mean for the work, and ``recommended`` true on the answer the turn \
leans towards (exactly one when ``multi_select`` is false, one or more when \
it is true; put it first). Where the answer space is open, enumerate the \
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
        json_schema=QUESTION_SCHEMA,
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
