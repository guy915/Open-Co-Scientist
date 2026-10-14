import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from co_scientist.domains.research_state.models import MetricDeltas, create_metrics_update
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.sandbox.skills import scoped_skill_usage
from co_scientist.science.citations import ReferenceIndex
from co_scientist.science.generation.assumptions import (
    generate_with_assumptions,
)
from co_scientist.science.generation.debate import generate_with_debate
from co_scientist.science.generation.expansion_research import (
    research_for_expansion,
)
from co_scientist.science.generation.literature_tools import (
    generate_with_tools,
)
from co_scientist.science.generation.operations import (
    GenerationCounts,
    _unpack_generation_results,
    finalize_generation,
    prepare_generation,
)

logger = logging.getLogger(__name__)


def _build_generation_tasks(
    state: WorkflowState,
    counts: GenerationCounts,
    articles_with_reasoning: str | None,
    reference_index: ReferenceIndex,
) -> list[tuple[str, Coroutine[Any, Any, Any]]]:
    tasks: list[tuple[str, Coroutine[Any, Any, Any]]] = []
    if counts.tools_count > 0:
        tasks.append(
            (
                "tools",
                generate_with_tools(state, counts.tools_count, reference_index),
            )
        )
    if counts.debate_with_lit_count > 0:
        tasks.append(
            (
                "debate_lit",
                generate_with_debate(
                    state=state,
                    count=counts.debate_with_lit_count,
                    articles_with_reasoning=articles_with_reasoning,
                    reference_index=reference_index,
                ),
            )
        )
    if counts.debate_only_count > 0:
        # Degraded debates must never inherit supplied literature context.
        tasks.append(
            (
                "debate_only",
                generate_with_debate(
                    state=state,
                    count=counts.debate_only_count,
                    articles_with_reasoning=None,
                    reference_index=ReferenceIndex(text="", sources={}),
                ),
            )
        )
    if counts.assumptions_count > 0:
        tasks.append(
            (
                "assumptions",
                generate_with_assumptions(
                    state,
                    counts.assumptions_count,
                    articles_with_reasoning=articles_with_reasoning,
                    reference_index=reference_index,
                ),
            )
        )
    return tasks


async def generate_hypotheses(state: WorkflowState) -> dict[str, Any]:
    """Generation failure is a hard error; returning partial/degraded output
    would hide a failed strategy."""
    logger.info("Starting hypothesis generation")

    expansion = await research_for_expansion(state)
    if expansion is not None:
        state = expansion.applied_to(state)

    plan = await prepare_generation(state)

    try:
        tasks = _build_generation_tasks(state, plan.counts, plan.literature, plan.reference_index)
        gathered = await asyncio.gather(*(task for _, task in tasks))
        results = _unpack_generation_results(tasks, gathered)
        result = await finalize_generation(state, plan.counts, results)
        if expansion is not None:
            result["articles"] = state.get("articles")
            result["research_ledgers"] = [expansion.ledger]
        return result

    except Exception as e:
        logger.error("Generation failed: %s", e)
        raise


async def generate_node(state: WorkflowState) -> dict[str, Any]:
    logger.info("Starting generate node")
    with scoped_skill_usage() as skills:
        result = await generate_hypotheses(state)
    result["metrics"] = create_metrics_update(
        hypothesis_count=result["hypothesis_count"],
        deltas=MetricDeltas(
            llm_calls=result.get("llm_call_count", 0),
            skills_used=skills.snapshot(),
        ),
    )
    logger.info(
        "Generate node complete: %s",
        result.get("message", "generated hypotheses"),
    )
    return result
