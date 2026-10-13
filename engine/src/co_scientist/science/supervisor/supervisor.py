import logging
from typing import Any

from co_scientist.core.constants import (
    EXTENDED_MAX_TOKENS,
    MEDIUM_TEMPERATURE,
    PROGRESS_SUPERVISOR_COMPLETE,
    PROGRESS_SUPERVISOR_START,
)
from co_scientist.domains.research_state.models import (
    MetricDeltas,
    create_metrics_update,
    phase_message,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
)
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.prompts import (
    PromptRunContext,
    SupervisorPromptInputs,
    get_supervisor_prompt,
)

logger = logging.getLogger(__name__)


async def supervisor_node(state: WorkflowState) -> dict[str, Any]:
    prompt_context = _extract_supervisor_context(state)
    await _announce_supervisor_start(state, prompt_context)

    supervisor_guidance = await _run_supervisor_planning(state, prompt_context)
    logger.info("Supervisor plan created")

    key_areas = _extract_key_areas(supervisor_guidance)
    _log_key_areas(key_areas)
    await emit_progress(
        state,
        "supervisor_complete",
        "Research plan created",
        PROGRESS_SUPERVISOR_COMPLETE,
        key_areas=len(key_areas),
    )

    return _build_supervisor_result(supervisor_guidance, key_areas)


async def _announce_supervisor_start(state: WorkflowState, prompt_context: dict[str, Any]) -> None:
    research_goal = prompt_context["inputs"].research_goal
    logger.info("Supervisor analyzing research goal (%s characters)", len(research_goal))
    await emit_progress(
        state,
        "supervisor_start",
        "Analyzing research goal and creating plan...",
        PROGRESS_SUPERVISOR_START,
    )


async def _run_supervisor_planning(
    state: WorkflowState, prompt_context: dict[str, Any]
) -> dict[str, Any]:
    prompt, schema = get_supervisor_prompt(**prompt_context)
    response = await call_llm_json(
        prompt=prompt,
        spec=CompletionSpec(
            role="supervisor",
            model_name=state["supervisor_model_name"],
            max_tokens=EXTENDED_MAX_TOKENS,
            temperature=MEDIUM_TEMPERATURE,
            json_schema=schema,
        ),
        options=LLMCallOptions(
            run_id=state.get("run_id"),
            prompt_name="supervisor",
        ),
    )
    return _build_supervisor_guidance(response)


def _log_key_areas(key_areas: list[str]) -> None:
    if key_areas:
        logger.info("Key research areas identified: %s", len(key_areas))


def _build_supervisor_result(
    supervisor_guidance: dict[str, Any], key_areas: list[str]
) -> dict[str, Any]:
    metrics = create_metrics_update(deltas=MetricDeltas(llm_calls=1))

    return {
        "supervisor_guidance": supervisor_guidance,
        "metrics": metrics,
        "messages": phase_message(
            "supervisor",
            "Created research plan and workflow guidance",
            key_areas=len(key_areas),
        ),
    }


def _extract_supervisor_context(state: WorkflowState) -> dict[str, Any]:
    """Capability flags must describe whether literature review can actually
    run rather than promising evidence unavailable to the workflow."""
    return {
        "inputs": SupervisorPromptInputs(
            research_goal=state["research_goal"],
            preferences=state.get("preferences"),
            attributes=state.get("attributes"),
            constraints=state.get("constraints"),
            criteria=state.get("criteria"),
            user_hypotheses=state.get("starting_hypotheses"),
            user_literature=state.get("literature"),
            initial_hypotheses_count=state.get("initial_hypotheses_count"),
            max_iterations=state.get("max_iterations"),
            evolution_max_count=state.get("evolution_max_count"),
            mcp_available=bool(state.get("mcp_available", False)),
            pubmed_available=bool(state.get("pubmed_available", False)),
        ),
        "context": PromptRunContext(
            tool_registry=state.get("tool_registry"),
            run_setup_guidance=state.get("run_setup_guidance"),
            run_focus_guidance=state.get("run_focus_guidance"),
        ),
    }


def _extract_key_areas(supervisor_guidance: dict[str, Any]) -> list[str]:
    """Provider responses may have unexpected shapes despite their schema."""
    goal_analysis = supervisor_guidance.get("research_goal_analysis", {})
    if not isinstance(goal_analysis, dict):
        return []
    key_areas: list[str] = goal_analysis.get("key_areas", [])
    return key_areas


def _build_supervisor_guidance(response: dict[str, Any]) -> dict[str, Any]:
    """Lax providers may omit fields; container defaults protect shared
    guidance consumers without repeating defensive checks."""
    return {
        "research_goal_analysis": response.get("research_goal_analysis", {}),
        "workflow_plan": response.get("workflow_plan", {}),
        "config_synthesis": response.get("config_synthesis", {}),
        "performance_assessment": response.get("performance_assessment", {}),
        "adjustment_recommendations": response.get("adjustment_recommendations", []),
        "output_preparation": response.get("output_preparation", {}),
    }
