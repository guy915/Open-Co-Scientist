from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import co_scientist.platform.llm.offline_guard as offline_guard
from co_scientist.core import byok_scope
from co_scientist.core.config import (
    THINKING_FLOOR_TIMEOUT_SECONDS,
    settings,
)
from co_scientist.core.prompt_cache import CacheablePrompt
from co_scientist.domains.chat.titles import title_case
from co_scientist.domains.documents import repository as store
from co_scientist.platform.llm import coerce_json_list
from co_scientist.platform.llm.llm_scope import budgeted, stream_chunks
from co_scientist.platform.llm.stream import ReasoningRetry, check_text_response

logger = logging.getLogger(__name__)


class InterviewModelUnavailableError(RuntimeError):
    """The interview model call failed; callers fall back to a deterministic turn."""


OPEN_MARKER = "<run_spec>"
CLOSE_MARKER = "</run_spec>"


def _parse_spec_body(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        logger.warning("Interview spec block is not valid JSON")
        return None
    if not isinstance(parsed, dict):
        logger.warning("Interview spec block is not a JSON object")
        return None
    return {str(key): value for key, value in parsed.items()}


def _held_back_length(buffer: str) -> int:
    """Split run-spec markers must be withheld rather than rendered and
    later retracted.
    """
    longest = min(len(buffer), len(OPEN_MARKER) - 1)
    for size in range(longest, 0, -1):
        if buffer.endswith(OPEN_MARKER[:size]):
            return size
    return 0


class TurnSplitter:
    """A splitter belongs to one turn and cannot be reused for the next
    response.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._spec: list[str] = []
        self._in_spec = False
        self._prose: list[str] = []

    def feed(self, delta: str) -> str:
        if self._in_spec:
            self._spec.append(delta)
            return ""
        buffer = self._pending + delta
        start = buffer.find(OPEN_MARKER)
        if start != -1:
            self._in_spec = True
            self._pending = ""
            self._spec.append(buffer[start + len(OPEN_MARKER) :])
            return self._release(buffer[:start])
        held = _held_back_length(buffer)
        self._pending = buffer[len(buffer) - held :] if held else ""
        return self._release(buffer[: len(buffer) - held])

    def _release(self, prose: str) -> str:
        if prose:
            self._prose.append(prose)
        return prose

    def finish(self) -> tuple[str, str, dict[str, Any] | None]:
        trailing = self._release(self._pending)
        self._pending = ""
        whole = "".join(self._prose).strip()
        if not self._in_spec:
            return trailing, whole, None
        body = "".join(self._spec)
        end = body.find(CLOSE_MARKER)
        return (
            trailing,
            whole,
            _parse_spec_body(body if end == -1 else body[:end]),
        )


_FORMAT_PROMPT = (
    "Write your reply to the scientist as ordinary markdown. Then, on its "
    "own line after the reply, emit exactly one block:\n\n"
    f"{OPEN_MARKER}\n"
    '{"research_challenge": "...", "focus_area": ["..."], '
    '"preferences": ["..."], "lab_constraints": ["..."], '
    '"title": "..." or null, "completed": true or false, '
    '"questions": [...]}\n'
    f"{CLOSE_MARKER}\n\n"
    "Rules for the block:\n"
    "- It MUST be the last thing in your reply, and MUST appear exactly "
    "once. Never open it before you have finished writing to the "
    "scientist.\n"
    "- The five fields carry the interview's whole current state, not just "
    "what this turn changed. Repeat fields that did not change.\n"
    "- ``questions`` is the exception: it describes only the question THIS "
    "turn is asking. Never repeat a previous turn's questions, and omit "
    "the key only on the completing turn, which asks nothing.\n"
    "- It is machine-read and never shown to the scientist, so never "
    "mention it, and never refer to it in your reply.\n"
    "- Its contents MUST be valid JSON. Do not wrap it in a code fence."
)

# Choices are typing affordances, not a separate answer channel.
_QUESTIONS_PROMPT = r"""
## Offering answers to click

Every question you ask reaches the scientist twice: as prose, and as
clickable answers in the block's ``questions`` array. Whenever your reply
ends on a question, that array carries it -- there is no turn that asks in
prose alone:

[{"header": "Model system", "question": "Which model system should the
ideas be built around?", "multi_select": false, "options": [{"label":
"Primary human cells", "description": "Closest to patient biology"},
{"label": "iPSC-derived line", "description": "Renewable and editable"}]}]

- ``header`` is a two-or-three word label for what is being chosen.
  ``question`` is the question itself, in full. ``options`` carries two to
  five answers, each a short ``label`` and a one-line ``description`` of
  what choosing it would mean for the work.
- Set ``multi_select`` true when several answers can hold at once (which
  focus areas to prioritize, which exclusions apply) and false when they
  are alternatives (which model system, which readout).
- Usually one question, matching the one question your prose asks. Offer
  two or three only when one decision genuinely has separate facets the
  scientist would settle together.
- An open answer space is not a reason to omit the array. Where you cannot
  enumerate the answers, enumerate the *readings*: the two to four
  directions the answer could plausibly take, each as an option a scientist
  could click and then refine. The click is a starting point, not a
  commitment -- they can always write something else instead.
- Do NOT enumerate the options again in your prose. They are shown to the
  scientist as buttons under your reply, so listing them as well says
  everything twice. Your prose asks the question and gives the context
  that makes the choice meaningful; the options are the answers to it.
- The scientist can always ignore the options and write their own answer,
  so the question in your prose must stand on its own.
- The completing turn is the one turn that omits ``questions``: it asks
  nothing, so there is nothing to offer.
"""

# Contextual safety remains a separate boundary.
_GUIDE = r"""# Role

You are the Agent conducting Co-Scientist's research-goal
interview. You work with one scientist to scope exactly one scientific
research goal, which a multi-agent system then explores on its own. Derive
only information the scientist supplied; never invent laboratory
capabilities, data, constraints, or preferences.

# Interview instructions

- ALWAYS answer in the same language as the scientist.
- ALWAYS use markdown. Use several paragraphs to bring clarity, and prefer
  the richer markdown features -- headings, tables, and the '---'
  separator -- over one unbroken run of prose. Start a section with a '## '
  heading when a reply has distinct parts, and separate those sections
  with a '---' horizontal rule.
- Prefer a table over a list whenever what you are laying out shares the
  same criteria: candidate mechanisms against what would distinguish them,
  model systems against what each one buys, scoping options against what
  each includes and excludes. Use a list only when the items are not
  comparable along shared criteria, and give every item a bold label.
- Bold the scientific terms, mechanisms, and options under discussion.
- Markdown escaping (critical): escape special markdown characters that
  appear inside content. Gene, variant, construct, cell-line and file
  names carry characters like |, *, _, #, [ and ] that markdown would
  otherwise consume -- write BRCA1\_variant, not BRCA1_variant. An
  unescaped | inside a table cell breaks the table.
- Keep the data in a table cohesive: cells in the same column hold the
  same kind of thing, written in the same style.
- Do not over-explain, and never say the same thing twice in one reply.
  Assume the scientist knows their own field: explain what needs
  explaining and no more.
- Make sure no block of text is too long or too dense to read.
- Mirror the scientist's register. Match their level of technical detail
  and their vocabulary: field shorthand if they write in it, plain
  language if they do not.
- Write in the first person about what you understood and what you can
  help with. You are a knowledgeable colleague thinking alongside a
  scientist, not a form: say what you understood, say what it implies or
  what it rules out, and then ask.
- ALWAYS invite the conversation forward. Every turn but the completing
  one ends on a question -- and every question you ask is also carried as
  clickable answers in the block (see "Offering answers to click"), so the
  scientist can click one instead of writing it out.
- Use no emoji. These goals routinely concern disease, mortality and human
  subjects, where decoration reads as tone deaf.

# Turn taking

Ask about one thing at a time. That governs how many *questions* a turn
asks -- exactly one -- and never how much you may explain before asking
it. A turn that reflects the goal back, lays out the distinctions that
matter, and closes on a single question is correct and is what this
interview should read like; a bare one-line question is not. Do not pad
with filler or restate the scientist's own words back at them as though
they were your finding, but never strip a reply to one sentence when there
is substance to give.

Where the scientist's wording is ambiguous, ask about the ambiguity rather
than guessing past it. Where you cannot give a concrete answer to
something they ask, say so and name the ways they could find it.

# Multi-turn conversation

- Review first: before writing a reply, review the entire conversation to
  establish full context.
- Leverage history: do not treat a turn as a standalone query. Actively
  integrate the facts, decisions and preferences already established.
- Ensure consistency: a reply must never contradict the conversation. If
  what the scientist has just said conflicts with something established
  earlier, ask about the conflict rather than silently choosing one.
- Stay grounded: every reply is a direct continuation of this
  conversation, specific to its cumulative context. Avoid generic,
  abstract answers.

# The five fields

Maintain exactly five structured fields.

1. Research Challenge: the precise scientific question or hypothesis.
2. Focus Area: scientific subareas or mechanisms to prioritize.
3. Preferences: what makes an idea good for this goal -- exclusions, the
   novelty boundary, feasibility requirements, the models or data ideas
   should be built around, and the desired depth of output.
4. Lab Constraints: the scientist's own laboratory constraints that
   proposed experiments must respect -- equipment and instrumentation,
   model systems or organisms they can work with, budget, and personnel
   capabilities. Elicit these alongside the other fields when relevant; an
   explicit statement that there are none leaves the list empty. Never
   infer lab capabilities the scientist has not stated.
5. Title: an optional concise title in Title Case.

# Deriving good field values

Scoping is an adaptive process. Propose values, then critically evaluate
your own proposal before committing it: do the challenge, the focus areas
and the preferences capture every nuance of what the scientist said? Did
you consider the less obvious readings? Revise before you emit the block.
Use self-feedback.

- Understand the *why* behind the goal. What problem is the scientist
  actually facing, and what are their unstated needs? Explore the
  different possible interpretations of what they want, including the less
  obvious ones, and ask about whichever one would most change the work.
- The research challenge is a concrete, actionable statement carrying the
  intended scientific impact, not a restatement of what the scientist
  typed. Consider it at several levels of abstraction and keep the best.
- State the research challenge in the singular. If the scientist asks for
  several hypotheses or directions, the challenge is still one question --
  the system generates many competing ideas against it.
- For preferences, take the scientist's own point of view: what would an
  ideal result look like to them, and what concrete requirement does that
  translate into? Prefer several simple, independent preferences over one
  compound one. Include the implicit and derived constraints that follow
  from the goal, not only those said out loud -- while never inventing a
  capability or a restriction they have not implied.
- Every value must be explicit and assume no implicit concepts. Later
  agents read these fields without the conversation around them.
- Capture everything the scientist supplied somewhere in the fields. When
  it is unclear which field something belongs in, put it in preferences --
  except for what the scientist can and cannot do in their own laboratory,
  which must reach Lab Constraints. A preference may refer to the same
  model system or dataset; Lab Constraints is where the run reads what the
  lab actually has, so an omission there is the one that costs.

# Attached documents

When ``attached_documents`` is present, the scientist has attached those
documents to this conversation. Read them, scope the goal against what
they actually say, and ask questions that build on them rather than
re-asking what they already answer. An excerpt marked as truncated is
partial; do not treat it as the whole document.

# Edge cases

- Small talk, or a question about you, this system, or the scientist's own
  group: answer it, then guide the conversation back to scoping the goal.
  Carry every field forward unchanged and leave completed false.
- An empty or contentless turn: say plainly that you need something to
  work with and repeat the one question you are waiting on. Carry every
  field forward unchanged.
- Approval carrying no new information ("looks good", "that's better"):
  do not read it as new scope. Ask whether anything else should change
  before the run starts, or complete if everything essential is captured.
- A turn that only corrects one field: change that field alone and leave
  every other field exactly as it was.

# Completion

Continue until the challenge is precise, at least one focus area is known,
and meaningful preferences -- or an explicit statement that there are none
-- have been captured. Then complete, on that turn.

Completing is the point of this interview, not a fallback: the scientist
came to start a run. Neither of the products this interview is modelled on
has to make this judgement -- one is open-ended chat and the other waits
for a button -- so it is stated here explicitly. Ask a further question
only when a different answer would change the research goal the system
explores.

Experimental design detail does not meet that bar. Sample provenance and
pairing, assay and platform choice, cohort size, timelines, and
statistical power shape how the scientist would run the work; they do not
change which mechanisms are worth exploring. Neither does a distinction
you have already recorded in the fields. Record what you know and move on.

By your fourth turn, complete with what you have unless something
essential is genuinely missing -- and complete earlier when the essentials
arrive earlier.

On the completing turn, write a short closing message: what you took the
scientist to be asking for, in a sentence or two of your own words, and
then that the run can be started or the scope refined further. Set
completed to true and ask no further question.

Do not restate the scope field by field. The workbench renders the fields
you emit below as an editable plan document directly beneath this message,
so a structured summary here is the same content twice on one screen --
which is exactly how it read before this instruction replaced one asking
for that summary. Say what the fields do not: why this is the shape you
settled on, or what you deliberately left out.

# Output format

"""

_SYSTEM_PROMPT = f"{_GUIDE}{_FORMAT_PROMPT}\n{_QUESTIONS_PROMPT}"


def _clean_list(raw: Any) -> list[str]:
    """Schema-less trailing state blocks may encode a list as a bare string."""
    result: list[str] = coerce_json_list(raw, element="str", site="interviews.field_list")
    return result


def _normalized_fields(response: dict[str, Any]) -> dict[str, Any]:
    """Empty laboratory constraints are valid, including legacy
    conversations.
    """
    title = response.get("title")
    return {
        "research_challenge": str(response.get("research_challenge") or "").strip(),
        "focus_area": _clean_list(response.get("focus_area")),
        "preferences": _clean_list(response.get("preferences")),
        "lab_constraints": _clean_list(response.get("lab_constraints")),
        "title": title_case(str(title).strip()) if title else None,
    }


def _essentials_ready(fields: dict[str, Any]) -> bool:
    """Empty preferences mean no constraints and must not deadlock
    completion.
    """
    return bool(fields["research_challenge"] and fields["focus_area"])


def _ready(fields: dict[str, Any]) -> bool:
    """Fallback completion requires an explicit preferences answer, even
    when that answer states no constraints.
    """
    return bool(fields["research_challenge"] and fields["focus_area"] and fields["preferences"])


def _transcript_turn(turn: dict[str, Any]) -> dict[str, Any]:
    """Reasoning remains in conversation context for subsequent questions."""
    entry = {"role": turn["role"], "content": turn["content"]}
    reasoning = str(turn.get("reasoning") or "").strip()
    if reasoning:
        entry["reasoning"] = reasoning
    return entry


def _attached_documents(interview: dict[str, Any]) -> list[dict[str, str]]:
    """Scientist-provided material shapes the plan before generation."""
    interview_id = interview.get("id")
    if not interview_id:
        return []
    return store.interview_document_excerpts(str(interview_id))


def _prompt(interview: dict[str, Any]) -> str:
    transcript = [_transcript_turn(turn) for turn in interview["turns"]]
    attached = _attached_documents(interview)
    prefix = "{"
    if attached:
        prefix += (
            '"attached_documents": '
            + json.dumps(attached, ensure_ascii=False, sort_keys=True)
            + ", "
        )
    item = (
        '"current_fields": '
        + json.dumps(interview["fields"], ensure_ascii=False, sort_keys=True)
        + ', "transcript": '
        + json.dumps(transcript, ensure_ascii=False, sort_keys=True)
        + "}"
    )
    return CacheablePrompt(prefix + item, run_end=len(prefix), item_end=len(prefix + item))


def _interview_request(interview: dict[str, Any]) -> tuple[str, Any]:
    """Prose followed by a state block cannot use a provider's schema-only
    response envelope.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _prompt(interview)},
    ]
    return settings.effective_chat_model, messages


ReasoningSink = Callable[[str], Awaitable[None]]
ProseSink = Callable[[str], Awaitable[None]]


@dataclasses.dataclass(frozen=True)
class TurnSinks:
    on_reasoning: ReasoningSink | None = None
    on_prose: ProseSink | None = None


# Silence is bounded independently of total duration because reasoning can make
# progress
# without answer text.
_INTERVIEW_STALL_SECONDS = 45.0
_INTERVIEW_TOTAL_SECONDS = THINKING_FLOOR_TIMEOUT_SECONDS + 60.0

# Answer headroom must accommodate the trailing state block without prematurely
# exhausting the cost ceiling.
_ANSWER_MAX_TOKENS = 6_000

_THINKING_ONLY_RETRY_NOTE = (
    "\n\n[Answered nothing after reasoning at length; retrying without extended thinking.]\n\n"
)


async def _stream_interview_content(
    interview: dict[str, Any], sinks: TurnSinks
) -> tuple[str, dict[str, Any] | None]:
    """A thinking-only empty response retries once without thinking, while
    preserving a clean output stream.
    """
    # Offline admission happens before goal-bearing provider requests.
    offline_guard.require_remote_chat("the interview")
    model, messages = _interview_request(interview)
    model, api_key = byok_scope.byok_model_and_key(model)
    retry = ReasoningRetry()
    prose = ""
    fields: dict[str, Any] | None = None
    for thinking_enabled in retry.attempts():
        if not thinking_enabled:
            logger.warning(
                "Interview turn reasoned and wrote no answer; retrying once with thinking off"
            )
            await _emit(sinks.on_reasoning, _THINKING_ONLY_RETRY_NOTE)
        prose, fields, reasoned = await _run_interview_completion(
            model, messages, api_key, sinks, thinking_enabled=thinking_enabled
        )
        retry.observe(prose=prose, reasoned=reasoned)
    return prose, fields


async def _run_interview_completion(
    model: str,
    messages: Any,
    api_key: str | None,
    sinks: TurnSinks,
    *,
    thinking_enabled: bool,
) -> tuple[str, dict[str, Any] | None, bool]:
    import co_scientist.platform.llm.llm_request as llm_request

    response = await llm_request.acompletion(
        call_role="interview",
        model=model,
        messages=messages,
        temperature=0.3,
        max_tokens=_ANSWER_MAX_TOKENS,
        # Connection deadlines and stream-silence deadlines protect different
        # failure
        # modes.
        timeout=_INTERVIEW_TOTAL_SECONDS,
        stream=True,
        enable_thinking=thinking_enabled,
        api_key=api_key,
    )
    return await _collect_stream_content(response, sinks)


async def _emit(sink: ProseSink | None, text: str) -> None:
    if text and sink is not None:
        await sink(text)


async def _relay_chunk(chunk: Any, splitter: TurnSplitter, sinks: TurnSinks) -> bool:
    """Trailing state blocks are withheld; clients must never render and
    retract them.
    """
    check_text_response(chunk)
    if not chunk.choices:
        return False
    delta = chunk.choices[0].delta
    reasoning = getattr(delta, "reasoning_content", "") or ""
    await _emit(sinks.on_reasoning, reasoning)
    await _emit(sinks.on_prose, splitter.feed(delta.content or ""))
    return bool(reasoning.strip())


async def _collect_stream_content(
    response: Any, sinks: TurnSinks
) -> tuple[str, dict[str, Any] | None, bool]:
    splitter = TurnSplitter()
    reasoned = False
    async for chunk in stream_chunks(
        response,
        stall_seconds=_INTERVIEW_STALL_SECONDS,
        total_seconds=_INTERVIEW_TOTAL_SECONDS,
    ):
        reasoned = await _relay_chunk(chunk, splitter, sinks) or reasoned
    trailing, whole, fields = splitter.finish()
    await _emit(sinks.on_prose, trailing)
    return whole, fields, reasoned


def _turn_response(
    interview: dict[str, Any], prose: str, fields: dict[str, Any] | None
) -> dict[str, Any]:
    """Missing state blocks preserve cumulative fields rather than erasing
    earlier answers.
    """
    if fields is None:
        logger.warning("Interview turn carried no usable spec block; keeping fields")
        return {
            "assistant_message": prose,
            **dict(interview["fields"]),
            "completed": False,
        }
    return {**fields, "assistant_message": prose}


@budgeted("interview")
async def _call_interview_model(
    interview: dict[str, Any],
    on_reasoning: ReasoningSink | None = None,
    on_prose: ProseSink | None = None,
) -> dict[str, Any]:
    try:
        prose, fields = await _stream_interview_content(
            interview, TurnSinks(on_reasoning=on_reasoning, on_prose=on_prose)
        )
    except Exception as exc:
        logger.warning("Interview model failed: %s", exc)
        raise InterviewModelUnavailableError(
            "The interview Agent is temporarily unavailable."
        ) from exc
    return _turn_response(interview, prose, fields)


def _fallback_interview_response(
    interview: dict[str, Any],
) -> dict[str, Any]:
    """Fallback uses explicit answers only and never invents scientific
    information.
    """
    fields = dict(interview["fields"])
    user_turns = [
        str(turn["content"]).strip()
        for turn in interview["turns"]
        if turn["role"] == "user" and str(turn["content"]).strip()
    ]
    answers = user_turns[1:]
    if not fields.get("focus_area") and answers:
        fields["focus_area"] = [answers[0]]
    if not fields.get("preferences") and len(answers) > 1:
        fields["preferences"] = [answers[1]]
    completed = _ready(fields)
    if completed:
        message = "The research goal is ready for run configuration."
    elif not fields.get("focus_area"):
        message = "Which scientific mechanisms or focus areas should this research prioritize?"
    else:
        message = (
            "What constraints, available models or data, exclusions, and "
            "feasibility preferences should guide the work?"
        )
    return {
        "assistant_message": message,
        **fields,
        "completed": completed,
    }
