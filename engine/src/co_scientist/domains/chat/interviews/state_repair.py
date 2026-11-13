from __future__ import annotations

import json
import logging
from typing import Any

import co_scientist.platform.llm.offline_guard as offline_guard
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.core.json_schema import obj, str_array
from co_scientist.core.prompt_cache import CacheablePrompt
from co_scientist.core.prompt_layout import render_cacheable_prompt
from co_scientist.domains.chat.interviews.questions import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    QUESTION_SCHEMA,
)
from co_scientist.platform.llm import CompletionSpec, LLMCallOptions, call_llm_json

logger = logging.getLogger(__name__)

# Some chat models write the reply and stop without the trailing state block.
# Without recovery the turn keeps the old fields and cannot complete, so a
# whole interview can stall on a model that never emits the block.
_STATE_SCHEMA = obj(
    {
        "research_challenge": {"type": "string"},
        "focus_area": str_array(),
        "preferences": str_array(),
        "lab_constraints": str_array(),
        "title": {"type": "string"},
        "completed": {"type": "boolean"},
        "questions": {"type": "array", "items": QUESTION_SCHEMA},
    }
)

_MAX_TOKENS = 1600

_INSTRUCTIONS = f"""\
A research-goal interview turn has just been written to a scientist, but the \
machine-readable state that should follow it is missing. Rebuild that state \
from the inputs.

- Start from ``current_fields``. Keep every value the scientist's latest \
answer and the turn do not change.
- Add or change a field only when the scientist's latest answer or the turn \
states it. Never invent scientific content.
- ``research_challenge`` is one sentence; ``focus_area``, ``preferences`` and \
``lab_constraints`` are short lists; ``title`` is a short title, or an empty \
string when none is known.
- ``completed`` is true only when the turn tells the scientist the goal is \
ready and asks nothing more.
- ``questions`` holds the question the turn ends on, if any, as clickable \
answers: the ``question`` in the turn's own words, a two-or-three word \
``header``, {MIN_OPTIONS} to {MAX_OPTIONS} ``options`` each with a short \
``label``, a one-line ``description`` and ``recommended`` (true on the \
answer the turn leans towards: exactly one unless ``multi_select``), and \
``multi_select``. Return an empty list when the turn asks nothing. Never \
invent a question.
"""


def _latest_user_answer(interview: dict[str, Any]) -> str:
    for turn in reversed(interview.get("turns") or []):
        if turn.get("role") == "user":
            return str(turn.get("content") or "").strip()
    return ""


def _repair_prompt(interview: dict[str, Any], message: str) -> CacheablePrompt:
    inputs = {
        "current_fields": interview.get("fields") or {},
        "latest_scientist_answer": _latest_user_answer(interview),
    }
    return render_cacheable_prompt(
        _INSTRUCTIONS,
        "",
        json.dumps(inputs, ensure_ascii=False, sort_keys=True)
        + "\n\nThe turn:\n---\n"
        + message.strip()
        + "\n---",
        "Rebuild the missing state. Never invent content or questions.",
    )


async def repair_turn_state(interview: dict[str, Any], message: str) -> dict[str, Any] | None:
    """Return the turn's state block rebuilt from its prose, or None.

    Never raises: a failed repair keeps the turn's earlier behaviour.
    """
    if not message.strip() or not offline_guard.remote_chat_allowed():
        return None
    model, api_key = byok_scope.byok_model_and_key(settings.effective_chat_model)
    spec = CompletionSpec(
        # Same short, non-reasoning interview repair budget as question repair.
        role="question_repair",
        model_name=model,
        max_tokens=_MAX_TOKENS,
        temperature=0,
        json_schema=_STATE_SCHEMA,
        api_key=api_key,
    )
    try:
        result = await call_llm_json(
            _repair_prompt(interview, message),
            spec,
            max_attempts=2,
            options=LLMCallOptions(prompt_name="interview_state_repair", enable_thinking=False),
        )
    except Exception:
        logger.warning("Interview state repair failed", exc_info=True)
        return None
    if not isinstance(result, dict):
        return None
    state = {str(key): value for key, value in result.items()}
    if not str(state.get("title") or "").strip():
        state["title"] = None
    return state
