import asyncio
import logging
import re
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from co_scientist.core.constants import (
    DEBATE_FINAL_TURN_MAX_TOKENS_CAP,
    DEBATE_FINAL_TURN_TOKENS_PER_HYPOTHESIS,
    EXTENDED_MAX_TOKENS,
    HIGH_TEMPERATURE,
    scaled_max_tokens,
)
from co_scientist.core.exceptions import GenerationError
from co_scientist.domains.research_state.models import GenerationMethod, Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm,
    call_llm_json,
)
from co_scientist.science.citations import (
    ReferenceIndex,
    hypothesis_from_llm_output,
)
from co_scientist.science.prompts import (
    DebatePromptRequest,
    PromptRunContext,
    get_debate_generation_prompt,
)
from co_scientist.science.prompts.generation_debate import (
    _DEBATE_MAX_DISCUSSION_TURNS,
)

logger = logging.getLogger(__name__)


# Parallel debates rotate angles to avoid converging on the same obvious
# hypothesis.


_DEBATE_DIVERSITY_ANGLES = [
    "a direct causal molecular or mechanistic intervention",
    "an upstream regulatory or control-system mechanism",
    "a downstream measurable phenotype or functional assay",
    "a cross-pathway interaction that could explain an unexpected effect",
    "a temporal or state-dependent mechanism that changes across conditions",
    "a translational experiment or intervention with practical constraints",
    "a falsification-focused hypothesis with a discriminating negative control",
    "a high-novelty mechanism that challenges the dominant explanation",
]


def _debate_diversity_instruction(debate_id: int | None, total_debates: int) -> str | None:

    if debate_id is None or total_debates <= 1:
        return None
    angle = _DEBATE_DIVERSITY_ANGLES[debate_id % len(_DEBATE_DIVERSITY_ANGLES)]
    return (
        f"Parallel debate {debate_id + 1} of {total_debates}: focus this "
        f"debate on {angle}. Produce a final hypothesis that is meaningfully "
        "different from what other parallel debates would generate; do not "
        "collapse to the most generic or obvious mechanism unless it uniquely "
        "fits this assigned angle."
    )


def _append_diversity_instruction(preferences: str | None, instruction: str | None) -> str | None:

    if not instruction:
        return preferences
    if preferences:
        return f"{preferences}\n\n{instruction}"
    return instruction


# Accept standalone conclusion markers, never quoted instructions or list
# labels.


_DEBATE_TERMINATOR = re.compile(r"\bHYPOTHESIS\b\s*[:.;\u2013\u2014-]")
_DEBATE_TERMINATOR_VARIANT = re.compile(r"(?im)^[ \t>*#\-]*HYPOTHESIS[ \t*]*(?::.*)?$")


def _debate_converged(response_text: str) -> bool:
    """False positives end discussion prematurely; false negatives only spend
    another turn, so marker matching stays conservative."""
    if _DEBATE_TERMINATOR.search(response_text):
        return True
    return bool(_DEBATE_TERMINATOR_VARIANT.search(response_text))


@dataclass(frozen=True)
class DebateBatchPosition:
    """Durable tasks need the whole fan-out position/size to preserve
    diversity angles, not their local count of one."""

    debate_index: int
    total_debates: int


def _first_debate_hypothesis_data(response: dict[str, Any], debate_label: str) -> dict[str, Any]:
    """A single debate still uses the list-wrapped schema shared by batch
    generation paths."""
    hypotheses_data: list[dict[str, Any]] = response.get("hypotheses", [])
    if not hypotheses_data:
        raise GenerationError(f"{debate_label} failed to generate hypothesis")
    return hypotheses_data[0]


async def _run_final_debate_turn(
    state: WorkflowState,
    ctx: "_DebateContext",
    turn: int,
    prompt: str,
    schema: Any,
) -> Hypothesis:
    # Each debate generates one hypothesis, so this budget is fixed despite the shared scaling.
    response = await call_llm_json(
        prompt=prompt,
        spec=CompletionSpec(
            role="generation",
            model_name=state["model_name"],
            max_tokens=scaled_max_tokens(
                EXTENDED_MAX_TOKENS,
                ctx.count,
                per_item=DEBATE_FINAL_TURN_TOKENS_PER_HYPOTHESIS,
                cap=DEBATE_FINAL_TURN_MAX_TOKENS_CAP,
            ),
            temperature=HIGH_TEMPERATURE,
            json_schema=schema,
        ),
        options=LLMCallOptions(
            run_id=state.get("run_id"),
            prompt_name=f"generate_debate_{ctx.debate_id}_final",
        ),
    )
    hyp_data = _first_debate_hypothesis_data(response, ctx.debate_label)

    return hypothesis_from_llm_output(
        hyp_data,
        ctx.ref_idx.sources,
        GenerationMethod.DEBATE,
        debate_id=ctx.debate_id,
    )


def _build_debate_turn_prompt(
    state: WorkflowState,
    ctx: "_DebateContext",
    transcript: str,
    is_final: bool,
) -> tuple[str, Any]:
    return get_debate_generation_prompt(
        DebatePromptRequest(
            research_goal=state["research_goal"],
            transcript=transcript,
            preferences=ctx.preferences,
            attributes=ctx.attributes,
            user_hypotheses=state.get("starting_hypotheses"),
            criteria=ctx.criteria,
            is_final_turn=is_final,
            articles_with_reasoning=ctx.articles_with_reasoning,
            articles=state.get("articles"),
            reference_list=ctx.ref_idx.text,
            context=PromptRunContext(
                supervisor_guidance=ctx.supervisor_guidance,
                meta_review=ctx.meta_review,
                tool_registry=state.get("tool_registry"),
                run_setup_guidance=state.get("run_setup_guidance"),
                run_focus_guidance=state.get("run_focus_guidance"),
            ),
        )
    )


@dataclass
class _DebateContext:
    ref_idx: ReferenceIndex
    debate_id: int | None
    debate_label: str
    supervisor_guidance: Any
    meta_review: Any
    preferences: str | None
    attributes: Any
    articles_with_reasoning: str | None
    criteria: list[str] | None = None
    count: int = 1


async def _run_debate_turns(
    state: WorkflowState,
    ctx: _DebateContext,
) -> tuple[Hypothesis, str, int]:
    """Final structured synthesis always runs, even when the discussion never
    reaches consensus."""
    transcript = ""
    turns_run = 0
    for turn in range(1, _DEBATE_MAX_DISCUSSION_TURNS + 1):
        prompt, _ = _build_debate_turn_prompt(state, ctx, transcript, is_final=False)
        response_text = await call_llm(
            prompt=prompt,
            spec=CompletionSpec(
                role="generation",
                model_name=state["model_name"],
                max_tokens=EXTENDED_MAX_TOKENS,
                temperature=HIGH_TEMPERATURE,
            ),
            options=LLMCallOptions(),
        )
        transcript += f"\n\nTurn {turn}:\n{response_text}"
        turns_run = turn
        if _debate_converged(response_text):
            logger.info("%s declared consensus after turn %s", ctx.debate_label, turn)
            break

    prompt, schema = _build_debate_turn_prompt(state, ctx, transcript, is_final=True)
    hypothesis = await _run_final_debate_turn(state, ctx, turns_run + 1, prompt, schema)

    return hypothesis, transcript, turns_run + 1


def _unpack_debate_results(
    debate_results: list[tuple[Hypothesis, str, int]],
) -> tuple[list[Hypothesis], list[dict[str, Any]], int]:
    """Persisted debate IDs identify the whole fan-out, unlike a durable
    task's local gather position."""
    debate_hypotheses = [hyp for hyp, _, _ in debate_results]
    debate_transcripts = [
        {
            "debate_id": (
                debate_hypotheses[i].debate_id if debate_hypotheses[i].debate_id is not None else i
            ),
            "transcript": transcript,
            "hypothesis_text": debate_hypotheses[i].text,
        }
        for i, (_, transcript, _) in enumerate(debate_results)
    ]
    llm_call_count = sum(calls for _, _, calls in debate_results)
    return debate_hypotheses, debate_transcripts, llm_call_count


def _build_debate_tasks(
    state: WorkflowState,
    count: int,
    articles_with_reasoning: str | None,
    reference_index: ReferenceIndex | None,
    batch_position: DebateBatchPosition,
) -> list[Coroutine[Any, Any, tuple[Hypothesis, str, int]]]:
    tasks = []
    for i in range(count):
        debate_id = batch_position.debate_index + i
        diversity_instruction = _debate_diversity_instruction(
            debate_id, batch_position.total_debates
        )
        ctx = _DebateContext(
            ref_idx=reference_index or ReferenceIndex(text="", sources={}),
            debate_id=debate_id,
            debate_label=f"debate {debate_id}",
            supervisor_guidance=state.get("supervisor_guidance"),
            meta_review=state.get("meta_review"),
            preferences=_append_diversity_instruction(
                state.get("preferences"), diversity_instruction
            ),
            attributes=state.get("attributes"),
            articles_with_reasoning=articles_with_reasoning,
            criteria=state.get("criteria"),
        )
        tasks.append(_run_debate_turns(state, ctx))
    return tasks


async def generate_with_debate(
    state: WorkflowState,
    count: int,
    articles_with_reasoning: str | None = None,
    reference_index: ReferenceIndex | None = None,
    batch_position: DebateBatchPosition | None = None,
) -> tuple[list[Hypothesis], list[dict[str, Any]], int]:

    if count == 0:
        return [], [], 0

    logger.info("Running %s parallel debates", count)

    if batch_position is None:
        position = DebateBatchPosition(0, count)
    else:
        position = DebateBatchPosition(
            max(0, batch_position.debate_index),
            max(
                batch_position.total_debates,
                batch_position.debate_index + count,
            ),
        )
    debate_tasks = _build_debate_tasks(
        state,
        count,
        articles_with_reasoning,
        reference_index,
        position,
    )
    debate_results = await asyncio.gather(*debate_tasks)
    debate_hypotheses, debate_transcripts, llm_call_count = _unpack_debate_results(debate_results)

    logger.info("Generated %s hypotheses from debates", len(debate_hypotheses))
    return debate_hypotheses, debate_transcripts, llm_call_count
