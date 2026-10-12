import asyncio
import logging
import random
from collections.abc import Coroutine
from typing import Any, Final

from co_scientist.core.constants import (
    EVOLVE_MAX_TOKENS_CAP,
    EVOLVE_TOKENS_PER_CONTEXT_HYPOTHESIS,
    EXTENDED_MAX_TOKENS,
    HIGH_TEMPERATURE,
    PROGRESS_EVOLVE_COMPLETE,
    PROGRESS_EVOLVE_START,
    scaled_max_tokens,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.models import Hypothesis, rank_by_elo
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
    indexed_prompt_name,
)
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.evolution.evolve_grounding import (
    enhancement_grounding_block,
    grounding_metrics_extra,
)
from co_scientist.science.evolution.evolve_prompt import (
    EvolutionOperator,
    select_operators,
)
from co_scientist.science.evolution.evolve_prompt import (
    _build_evolution_prompt as _build_evolution_prompt,
)
from co_scientist.science.evolution.evolve_prompt import (
    _EvolutionOperation as _EvolutionOperation,
)
from co_scientist.science.evolution.evolve_prompt import (
    _specialist_feedback_for as _specialist_feedback_for,
)
from co_scientist.science.evolution.evolve_prompt import (
    combination_partners as combination_partners,
)
from co_scientist.science.evolution.evolve_prompt import (
    find_nearest_peer as find_nearest_peer,
)
from co_scientist.science.evolution.evolve_prompt import (
    sample_context_hypotheses as sample_context_hypotheses,
)
from co_scientist.science.evolution.evolve_results import (
    _apply_evolution_result as _apply_evolution_result,
)
from co_scientist.science.evolution.evolve_results import (
    _build_evolve_state_delta,
)
from co_scientist.science.evolution.evolve_results import (
    _collect_evolution_results as _collect_evolution_results,
)
from co_scientist.science.evolution.operations import (
    EvolutionContext,
    build_evolution_context,
)

logger = logging.getLogger(__name__)


# SSR section 4 uses fixed top-five parents; tier-scaled envelopes would change
# scientific selection rather than just fund compute.
EVOLUTION_PARENT_COUNT: Final = 5


def _select_evolution_pool(
    hypotheses: list[Hypothesis],
) -> list[Hypothesis]:
    """Rank defensively after early exits; exclude blocked and undermined
    parents so unsupported leaders cannot seed later generations."""
    rankable = [hyp for hyp in hypotheses if hyp.is_rankable() and not hyp.is_undermined()]
    if not rankable:
        logger.warning(
            "Evolution has no parents: 0 of %s hypotheses are eligible",
            len(hypotheses),
        )
    return rank_by_elo(rankable)[:EVOLUTION_PARENT_COUNT]


async def _emit_evolution_start(state: WorkflowState, actual_count: int) -> None:
    logger.info("Evolving top %s hypotheses", actual_count)

    await emit_progress(
        state,
        "evolve_start",
        f"Evolving top {actual_count} hypotheses...",
        PROGRESS_EVOLVE_START,
    )

    logger.info(
        "Evolving %s hypotheses with strategic context sampling "
        "(max 15 context hypotheses per evolution)",
        actual_count,
    )


async def _prepare_evolution_round(
    state: WorkflowState,
    hypotheses: list[Hypothesis],
) -> tuple[list[Hypothesis], list[str], dict[str, Any] | None]:
    top_k = _select_evolution_pool(hypotheses)
    await _emit_evolution_start(state, len(top_k))

    # Include pruned duplicate text so evolution cannot recreate earlier
    # removals.
    removed_duplicates = [dup.get("text", "") for dup in state.get("removed_duplicates", [])]
    supervisor_guidance = state.get("supervisor_guidance")

    return top_k, removed_duplicates, supervisor_guidance


async def _finalize_evolve_result(
    state: WorkflowState,
    children: list[Hypothesis],
    evolution_details: list[dict[str, Any]],
    attempt_count: int,
    extra_llm_calls: int = 0,
) -> dict[str, Any]:
    # Append children without retiring parents so both can compete in the next
    # tournament.
    logger.info(
        "Evolution produced %s new children from %s attempts",
        len(children),
        attempt_count,
    )

    await emit_progress(
        state,
        "evolve_complete",
        f"Evolved {len(children)} new child hypotheses",
        PROGRESS_EVOLVE_COMPLETE,
        evolved_count=len(children),
    )

    return _build_evolve_state_delta(children, evolution_details, attempt_count + extra_llm_calls)


_build_evolution_context = build_evolution_context

_DEFAULT_EVOLUTION_OPERATION = _EvolutionOperation()

# Combination, inspiration and analogy operators need their designated partners;
# otherwise their template input renders empty.
_PARTNER_OPERATORS = frozenset(
    {
        EvolutionOperator.COMBINATION,
        EvolutionOperator.INSPIRATION,
        EvolutionOperator.OUT_OF_BOX,
    }
)


def _evolve_token_budget(other_hypotheses_texts: list[str]) -> int:
    """Capped peer sampling bounds prompt cost independently of whole-pool
    size."""
    evolve_max_tokens = scaled_max_tokens(
        EXTENDED_MAX_TOKENS,
        len(other_hypotheses_texts),
        per_item=EVOLVE_TOKENS_PER_CONTEXT_HYPOTHESIS,
        cap=EVOLVE_MAX_TOKENS_CAP,
    )
    logger.debug(
        "evolution token budget: %s (%s context hypotheses)",
        evolve_max_tokens,
        len(other_hypotheses_texts),
    )
    return evolve_max_tokens


async def _call_evolution_llm(
    full_prompt: str,
    schema: dict[str, Any] | None,
    other_hypotheses_texts: list[str],
    context: EvolutionContext,
    hypothesis_index: int | None,
) -> dict[str, Any]:
    evolve_max_tokens = _evolve_token_budget(other_hypotheses_texts)
    prompt_name = indexed_prompt_name("evolve", hypothesis_index)
    return await call_llm_json(
        prompt=full_prompt,
        spec=CompletionSpec(
            role="evolution",
            model_name=context.model_name,
            max_tokens=evolve_max_tokens,
            temperature=HIGH_TEMPERATURE,
            json_schema=schema,
        ),
        max_attempts=7,
        options=LLMCallOptions(
            prompt_name=prompt_name,
        ),
    )


async def evolve_single_hypothesis(
    hypothesis: Hypothesis,
    other_hypotheses: list[Hypothesis],
    context: EvolutionContext,
    hypothesis_index: int | None = None,
    operation: _EvolutionOperation = _DEFAULT_EVOLUTION_OPERATION,
) -> tuple[Hypothesis | None, dict[str, Any] | None]:
    """Pool peers resist convergence and provide near-duplicate rejection."""
    response = await _evolve_llm_response(
        hypothesis,
        other_hypotheses,
        context,
        hypothesis_index,
        operation,
    )
    return _apply_evolution_result(
        hypothesis,
        response,
        other_hypotheses,
        context,
        operation,
    )


async def _evolve_llm_response(
    hypothesis: Hypothesis,
    other_hypotheses: list[Hypothesis],
    context: EvolutionContext,
    hypothesis_index: int | None,
    operation: _EvolutionOperation,
) -> dict[str, Any]:
    other_hypotheses_texts = [peer.text for peer in other_hypotheses]
    grounding = ""
    if operation.operator is EvolutionOperator.ENHANCEMENT and context.state is not None:
        grounding = await enhancement_grounding_block(context.state, hypothesis)
    full_prompt, schema = _build_evolution_prompt(
        hypothesis,
        other_hypotheses_texts,
        context,
        operation,
        grounding,
    )
    response = await _call_evolution_llm(
        full_prompt,
        schema,
        other_hypotheses_texts,
        context,
        hypothesis_index,
    )
    return {**response, "_evolution_operator": operation.operator.value}


def _context_sample_seed(context: EvolutionContext, hypothesis: Hypothesis) -> str:
    """Stable run/parent IDs preserve retry samples without concurrent runs
    perturbing a shared RNG."""
    return f"{context.run_id or 'evolution'}:context:{hypothesis.id}"


async def _evolve_or_none(
    hypothesis: Hypothesis,
    other_hypotheses: list[Hypothesis],
    context: EvolutionContext,
    hypothesis_index: int,
    operation: _EvolutionOperation,
) -> tuple[Hypothesis | None, dict[str, Any] | None]:
    """One failed parent must not abort paid-for sibling children or make
    durable retries regenerate the whole round."""
    try:
        return await evolve_single_hypothesis(
            hypothesis=hypothesis,
            other_hypotheses=other_hypotheses,
            context=context,
            hypothesis_index=hypothesis_index,
            operation=operation,
        )
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as e:
        logger.error("Evolution failed for hypothesis %s: %s", hypothesis_index, e)
        return None, None


def _build_single_evolution_task(
    state: WorkflowState,
    i: int,
    hyp: Hypothesis,
    context: EvolutionContext,
    operator: EvolutionOperator,
) -> Coroutine[Any, Any, tuple[Hypothesis | None, dict[str, Any] | None]]:
    partners = (
        tuple(combination_partners(context.ranked_hypotheses, hyp))
        if operator in _PARTNER_OPERATORS
        else ()
    )
    operation = _EvolutionOperation(
        operator=operator,
        specialist_feedback=_specialist_feedback_for(state, hyp),
        partners=partners,
    )
    # Whole-pool peers expose duplicates beyond top-k and exercise capped
    # diversity sampling even with small parent sets.
    return _evolve_or_none(
        hyp,
        sample_context_hypotheses(
            all_hypotheses=state["hypotheses"],
            exclude_hypothesis=hyp,
            max_context=15,
            ranked_hypotheses=list(context.ranked_hypotheses),
            rng=random.Random(_context_sample_seed(context, hyp)),
        ),
        context,
        i,
        operation,
    )


def _build_evolution_tasks(
    state: WorkflowState,
    top_k: list[Hypothesis],
    removed_duplicates: list[str],
    supervisor_guidance: dict[str, Any] | None,
    operators: list[EvolutionOperator],
) -> list[Coroutine[Any, Any, tuple[Hypothesis | None, dict[str, Any] | None]]]:
    context = build_evolution_context(state, removed_duplicates, supervisor_guidance)
    # Sample from one round-ranked pool so removing a parent cannot reorder
    # peers.
    return [
        _build_single_evolution_task(state, i, hyp, context, operator)
        for i, (hyp, operator) in enumerate(zip(top_k, operators, strict=True))
    ]


async def evolve_node(state: WorkflowState) -> dict[str, Any]:
    hypotheses = state["hypotheses"]

    (
        top_k,
        removed_duplicates,
        supervisor_guidance,
    ) = await _prepare_evolution_round(state, hypotheses)

    operators = select_operators(
        len(top_k),
        state.get("current_iteration", 0),
        state.get("run_id") or "evolution",
    )

    evolution_tasks = _build_evolution_tasks(
        state, top_k, removed_duplicates, supervisor_guidance, operators
    )
    results = await asyncio.gather(*evolution_tasks)

    children, evolution_details = _collect_evolution_results(results)

    return await _finalize_evolve_result(
        state,
        children,
        evolution_details,
        attempt_count=len(top_k),
        extra_llm_calls=grounding_metrics_extra(state, [operator.value for operator in operators]),
    )
