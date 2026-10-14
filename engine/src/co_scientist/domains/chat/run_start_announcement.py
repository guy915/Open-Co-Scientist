from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from time import perf_counter
from typing import Any

from co_scientist.core import byok_scope
from co_scientist.core.config import (
    THINKING_FLOOR_TIMEOUT_SECONDS,
    settings,
)
from co_scientist.core.prompt_cache import CacheablePrompt
from co_scientist.core.sse import sse_frame
from co_scientist.domains.chat.repository import messages as store
from co_scientist.domains.chat.repository.messages import NewMessage
from co_scientist.platform.db.models import RunRow
from co_scientist.platform.llm import offline_guard
from co_scientist.platform.llm.llm_scope import budgeted_stream, stream_chunks
from co_scientist.platform.llm.stream import ReasoningRetry, check_text_response
from co_scientist.platform.telemetry.diagnostic_events import log_chat_turn
from co_scientist.platform.telemetry.logging_setup import run_log_context

logger = logging.getLogger(__name__)

START_KIND = "start"

# Fund reasoning headroom as well as the two-sentence answer; answer-only limits
# can produce an empty completion.
_ANNOUNCEMENT_MAX_TOKENS = 600
# Bound silence separately: a long reasoning stream still gives the scientist
# visible progress.
_STALL_SECONDS = 45.0
_TOTAL_SECONDS = THINKING_FLOOR_TIMEOUT_SECONDS + 60.0

# The run started regardless of announcement availability; standby copy must
# confirm that fact rather than imply failure.
FALLBACK_ANNOUNCEMENT = (
    "Your session has been started and Co-Scientist has started research!"
    "\n\n"
    "You can view and interact with your session at any time, but note that "
    "it might take a few minutes for the first ideas to be ready to view."
)

_SYSTEM_PROMPT = """You are the Agent in Co-Scientist.

The scientist has just started a research session. The multi-agent system is
already exploring their goal on its own, and a card naming the session --
with every action available on it -- is shown directly beneath your reply.

Write exactly two sentences telling them so.

- The first says the session has started and that research on their goal is
  under way. Name the goal in a clause of your own words; never quote it back
  in full, and never restate the plan.
- The second says they can open the session and interact with it whenever
  they like, and that it takes a few minutes before the first ideas are ready
  to view.

Rules:
- Plain prose, and nothing else. No headings, no lists, no bold, no emoji.
- Ask no question and offer nothing further. The interview is over, and the
  card below your reply carries what happens next.
- Address the scientist directly, in the same language they wrote in.
- Two sentences, under 60 words in total."""


def _announcement_prompt(run: RunRow) -> str:
    """Omit plan fields already shown on the card so the model confirms the
    start rather than reciting setup.
    """
    prefix = '{"research_goal": ' + json.dumps(run.research_goal, ensure_ascii=False)
    item = (
        ', "session_title": ' + json.dumps(run.title, ensure_ascii=False) if run.title else ""
    ) + "}"
    return CacheablePrompt(prefix + item, run_end=len(prefix), item_end=len(prefix + item))


def _delta_text(chunk: Any) -> tuple[str, str]:
    check_text_response(chunk)
    if not chunk.choices:
        return "", ""
    delta = chunk.choices[0].delta
    reasoning = getattr(delta, "reasoning_content", None) or ""
    return str(reasoning), str(delta.content or "")


async def _stream_model_fragments(
    run: RunRow, *, thinking_enabled: bool = True
) -> AsyncGenerator[tuple[str, str], None]:
    from co_scientist.platform.llm import llm_request

    # Admit before shaping requests because the scientist's goal is sent
    # verbatim.
    offline_guard.require_remote_chat("the session announcement")
    model, api_key = byok_scope.byok_model_and_key(settings.effective_chat_model)
    response = await llm_request.acompletion(
        call_role="announcement",
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _announcement_prompt(run)},
        ],
        temperature=0.7,
        max_tokens=_ANNOUNCEMENT_MAX_TOKENS,
        timeout=_TOTAL_SECONDS,
        stream=True,
        enable_thinking=thinking_enabled,
        api_key=api_key,
    )
    async for chunk in stream_chunks(
        response, stall_seconds=_STALL_SECONDS, total_seconds=_TOTAL_SECONDS
    ):
        reasoning, prose = _delta_text(chunk)
        if reasoning:
            yield "reasoning", reasoning
        if prose:
            yield "chunk", prose


async def replay_announcement(run_id: str, prompt_id: int) -> AsyncGenerator[str, None]:
    reply = next(
        (
            message
            for message in store.list_messages(run_id)
            if message.kind == START_KIND and message.sender == "system"
        ),
        None,
    )
    text = reply.content if reply else FALLBACK_ANNOUNCEMENT
    fallback = bool((reply.meta or {}).get("fallback")) if reply else True
    yield sse_frame({"type": "chunk", "content": text})
    yield sse_frame({"type": "done", "prompt_id": prompt_id, "fallback": fallback})


def _persist_announcement(run_id: str, text: str, reasoning: str, fallback: bool) -> None:
    """Keep reasoning with the durable reply so reopened chats reproduce the
    disclosure shown live.
    """
    meta: dict[str, Any] = {}
    if reasoning:
        meta["reasoning"] = reasoning
    if fallback:
        meta["fallback"] = True
    store.append_message(
        NewMessage(
            run_id=run_id,
            sender="system",
            content=text,
            kind=START_KIND,
            meta=meta or None,
        )
    )


async def _relay_announcement(
    run: RunRow,
    prose: list[str],
    reasoning: list[str],
    *,
    thinking_enabled: bool = True,
) -> AsyncGenerator[str, None]:
    async for kind, fragment in _stream_model_fragments(run, thinking_enabled=thinking_enabled):
        (reasoning if kind == "reasoning" else prose).append(fragment)
        yield sse_frame({"type": kind, "content": fragment})


async def _announcement_attempts(
    run: RunRow,
    byok: byok_scope.ByokCredential | None,
    prose: list[str],
    reasoning: list[str],
) -> AsyncGenerator[str, None]:
    """A clean stream ending with reasoning alone gets one thinking-off
    retry; that is distinct from provider failure.
    """
    retry = ReasoningRetry()
    with byok_scope.scoped_byok(byok):
        for thinking_enabled in retry.attempts():
            if not thinking_enabled:
                logger.info(
                    "session announcement reasoned and wrote nothing; retrying without thinking"
                )
            async for frame in _relay_announcement(
                run, prose, reasoning, thinking_enabled=thinking_enabled
            ):
                yield frame
            retry.observe(prose="".join(prose), reasoned=bool("".join(reasoning).strip()))


@budgeted_stream("announcement")
async def stream_announcement(
    run: RunRow,
    prompt_message_id: int,
    byok: byok_scope.ByokCredential | None = None,
) -> AsyncGenerator[str, None]:
    """The run already started; announcement failure must yield standby
    confirmation rather than imply scientific execution failed.
    """
    started = perf_counter()
    prose: list[str] = []
    reasoning: list[str] = []
    try:
        async for frame in _announcement_attempts(run, byok, prose, reasoning):
            yield frame
    except Exception as exc:
        with run_log_context(run.id):
            logger.info(
                "session announcement for run %s falls back (%s)", run.id, type(exc).__name__
            )
    text = "".join(prose).strip()
    fallback = not text
    if fallback:
        text = FALLBACK_ANNOUNCEMENT
        yield sse_frame({"type": "chunk", "content": text})
    _persist_announcement(run.id, text, "".join(reasoning).strip(), fallback)
    log_chat_turn("agent", text, run_id=run.id, duration_seconds=perf_counter() - started)
    yield sse_frame({"type": "done", "prompt_id": prompt_message_id, "fallback": fallback})
