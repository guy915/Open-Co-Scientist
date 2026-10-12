from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.domains.chat.titles import title_case
from co_scientist.platform.llm import llm_request, offline_guard
from co_scientist.platform.llm.llm_scope import budgeted
from co_scientist.platform.llm.request.response import extract_token_usage
from co_scientist.platform.llm.request.thinking import (
    thinking_safe_timeout,
)
from co_scientist.platform.llm.stream import ReasoningRetry, check_text_response

logger = logging.getLogger(__name__)

_MAX_TITLE_CHARS = 80
_MAX_RESTATEMENT_CHARS = 800

# Single-goal input, so the prompt carries no conversation or attachment rules.
_TITLE_PROMPT = (
    "You generate the sidebar title for a research session. Given the "
    "session's research goal, reply with a short title summarizing its "
    "subject.\n"
    "\n"
    "Rules:\n"
    "- The title MUST be 3 to 6 words and MUST summarize the goal, not "
    "repeat its opening words.\n"
    "- Put the most distinctive words first: the disease, organism, "
    "molecule, method, or system the goal is about. DO NOT begin with an "
    "article or a preposition.\n"
    "- DO NOT begin with the goal's framing verb or question word, such as "
    "'Find', 'Propose', 'Develop', 'Investigate', 'How', or 'What'.\n"
    "- Use Title Case. Write the title in the same language as the goal; "
    "where the goal has typos, infer the intended wording.\n"
    "- DO NOT use colons, quotation marks, or trailing punctuation.\n"
    "- DO NOT use words like 'research', 'goal', 'study', 'session', or "
    "'title' unless they carry real meaning in the goal.\n"
    "- Reply with the title alone, as a standalone string: no preamble, no "
    "explanation, no surrounding data structure.\n"
    "\n"
    "Examples:\n"
    "\n"
    "Goal: Find drug repurposing candidates that could slow the "
    "progression of amyotrophic lateral sclerosis.\n"
    "Title: Drug Repurposing in ALS\n"
    "\n"
    "Goal: What mechanisms allow senescent cells to escape immune "
    "clearance in aged tissue?\n"
    "Title: Senescent Cell Immune Escape\n"
    "\n"
    "Goal: Propose experiments testing whether ferroptosis regulators can "
    "be targeted in pancreatic cancer.\n"
    "Title: Ferroptosis Targets in Pancreatic Cancer\n"
    "\n"
    "Goal: How does antibiotic resistance emerge in Pseudomonas "
    "aeruginosa biofilms, and how might it be disrupted?\n"
    "Title: Pseudomonas Biofilm Antibiotic Resistance\n"
    "\n"
    "Goal: Develop a computational model of tau propagation across "
    "cortical networks.\n"
    "Title: Tau Propagation Network Modeling"
)

_RESTATEMENT_PROMPT = (
    "You restate a scientific research goal for the top of a research "
    "report. Given the goal, reply with a fresh narrative restatement of "
    "it.\n"
    "\n"
    "Rules:\n"
    "- Write 1 to 3 sentences of plain prose, one paragraph, describing "
    "what the research seeks to discover or explain.\n"
    "- Restate the goal in DIFFERENT words -- do not repeat its phrasing or "
    "echo its opening clause.\n"
    "- Write in the third person about the research itself; do not address "
    "the reader, and do not refer to yourself, the report, or 'the goal'.\n"
    "- Do NOT add any heading, label, preamble, bullet, or quotation "
    "marks; reply with the paragraph alone.\n"
    "- Write in the same language as the goal; where the goal has typos, "
    "infer the intended wording.\n"
    "\n"
    "Example:\n"
    "\n"
    "Goal: Find drug repurposing candidates that could slow the "
    "progression of amyotrophic lateral sclerosis.\n"
    "Restatement: This investigation seeks existing, already-approved "
    "drugs that might be redirected to decelerate the neurodegeneration "
    "characteristic of ALS, identifying candidates whose known mechanisms "
    "plausibly intersect the disease's progression."
)


@dataclass(frozen=True)
class _TextRequest:
    purpose: str
    system_prompt: str
    max_tokens: int
    timeout_seconds: float
    temperature: float


# Token caps leave headroom over the character bounds: a capped Haiku reply
# falls through to paid Azure.
_TITLE = _TextRequest("run titling", _TITLE_PROMPT, 40, 15.0, 0.3)
_RESTATEMENT = _TextRequest("goal restatement", _RESTATEMENT_PROMPT, 260, 20.0, 0.4)


def _clean_text(raw: str, max_chars: int, punctuation: str = "") -> str | None:
    text = " ".join(raw.strip().split()).strip("\"'").strip()
    if punctuation:
        text = text.rstrip(punctuation).strip()
    return text if text and len(text) <= max_chars else None


def clean_title(raw: str) -> str | None:
    text = _clean_text(raw, _MAX_TITLE_CHARS, ".!?,;:")
    return title_case(text) if text else None


async def _request_completion(
    goal: str, request: _TextRequest, *, thinking_enabled: bool = True
) -> Any:
    """The goal is sent verbatim, so offline admission precedes request
    construction; both attempts keep their token and timeout floors.
    """
    offline_guard.require_remote_chat(request.purpose)
    model, api_key = byok_scope.byok_model_and_key(settings.effective_chat_model)
    return await asyncio.wait_for(
        llm_request.acompletion(
            call_role="goal_text",
            model=model,
            messages=[
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": goal},
            ],
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            enable_thinking=thinking_enabled,
            api_key=api_key,
        ),
        timeout=thinking_safe_timeout(model, request.timeout_seconds),
    )


async def _generate_text(goal: str, request: _TextRequest) -> str | None:
    goal = goal.strip()
    if not goal:
        return None
    retry = ReasoningRetry()
    content = ""
    for thinking_enabled in retry.attempts():
        if not thinking_enabled:
            logger.warning(
                "%s call reasoned and wrote no answer; retrying once with thinking off",
                request.purpose,
            )
        try:
            response = await _request_completion(goal, request, thinking_enabled=thinking_enabled)
            check_text_response(response)
        except Exception as exc:
            logger.warning("%s generation failed: %s", request.purpose, exc)
            return None
        content = _response_content(response)
        choices = getattr(response, "choices", None) or []
        retry.observe(
            prose=content,
            reasoned=extract_token_usage(response).reasoning_tokens > 0,
            tool_requested=bool(choices and getattr(choices[0].message, "tool_calls", None)),
        )
    return content


@budgeted("title")
async def generate_run_title(goal: str) -> str | None:
    content = await _generate_text(goal, _TITLE)
    return clean_title(content) if content is not None else None


@budgeted("goal_restatement")
async def generate_goal_restatement(goal: str) -> str | None:
    content = await _generate_text(goal, _RESTATEMENT)
    return _clean_text(content, _MAX_RESTATEMENT_CHARS) if content is not None else None


def _response_content(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    return str(choices[0].message.content or "")
