"""Execution supplies observations, never verdicts; fallback is mental
review. The caller attests whether execution occurred."""

from __future__ import annotations

import logging
from typing import Any

from co_scientist.core.constants import (
    EXTENDED_MAX_TOKENS,
    LOW_TEMPERATURE,
    truncate_for_prompt,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    ToolLoop,
    call_llm_with_tools,
)
from co_scientist.platform.sandbox.workspace.run_workspace import open_review_workspace
from co_scientist.platform.sandbox.workspace.tool_schemas import RUN_COMMAND
from co_scientist.platform.sandbox.workspace.tools import (
    WorkspaceToolProvider,
    workspace_tool_schemas,
)
from co_scientist.science.prompts import load_prompt

logger = logging.getLogger(__name__)

# Writing, running, repairing and sweeping a model needs multiple round trips; a
# crash alone spends two, so a tiny turn ceiling can stop before execution.
MAX_SIMULATION_TURNS = 14

# The measured 45k margin supports completion; larger budgets added cost.
# Estimates omit resent schemas; provider billing measured about 2.5x higher.
SIMULATION_TOKEN_BUDGET = 45_000

# The same capped observation enters both the prompt and checkpointed
# enrichments; neither should inherit the full tool-loop output budget.
MAX_OBSERVATION_CHARS = 12000


_NO_NETWORK_NOTE = (
    " There is no network access and nothing outside this directory is "
    "reachable, so use only the standard library."
)


def _tool_provider(
    run_id: str, hypothesis_id: str
) -> tuple[WorkspaceToolProvider, list[dict[str, Any]]] | None:
    """No confinement means no execution tool. Network/skills distract from
    simulation and stay off."""
    session = open_review_workspace(run_id, hypothesis_id)
    schemas = workspace_tool_schemas(session.policy)
    if not any(schema.get("function", {}).get("name") == RUN_COMMAND for schema in schemas):
        return None
    return WorkspaceToolProvider(session), schemas


async def simulation_observations(state: WorkflowState, hypothesis: Hypothesis) -> str | None:
    run_id = str(state.get("run_id") or "unknown")
    try:
        opened = _tool_provider(run_id, hypothesis.id)
    except Exception as exc:
        # Workspace creation is outside the loop guard; disk failures need
        # mental fallback rather than exhausted durable retries.
        logger.warning("simulation workspace unavailable for %s: %s", hypothesis.id, exc)
        return None
    if opened is None:
        logger.info(
            "simulation execution unavailable for %s; reviewing by mental simulation",
            hypothesis.id,
        )
        return None
    observations = await _run_simulation_loop(state, hypothesis, opened)
    if not observations:
        return None
    return truncate_for_prompt(observations, MAX_OBSERVATION_CHARS)


async def _run_simulation_loop(
    state: WorkflowState,
    hypothesis: Hypothesis,
    opened: tuple[WorkspaceToolProvider, list[dict[str, Any]]],
) -> str:
    provider, schemas = opened
    try:
        return await _observe(state, hypothesis, provider, schemas)
    finally:
        # Close orphaned commands when the loop ends; guard finally cleanup so
        # its failure cannot replace a successful observation.
        try:
            await provider.session.close()
        except Exception as exc:
            logger.warning(
                "could not close the simulation workspace for %s: %s",
                hypothesis.id,
                exc,
            )


async def _observe(
    state: WorkflowState,
    hypothesis: Hypothesis,
    provider: WorkspaceToolProvider,
    schemas: list[dict[str, Any]],
) -> str:
    prompt = load_prompt(
        "simulation_execution",
        {
            "research_goal": state["research_goal"],
            "hypothesis_text": hypothesis.text,
            "environment_note": _NO_NETWORK_NOTE,
        },
    )
    try:
        observations, _ = await call_llm_with_tools(
            prompt=prompt,
            spec=CompletionSpec(
                role="simulation",
                model_name=state["model_name"],
                max_tokens=EXTENDED_MAX_TOKENS,
                temperature=LOW_TEMPERATURE,
            ),
            # Turn limits cannot bound transcript spend: each call resends accumulated text.
            loop=ToolLoop(
                tools=schemas,
                executor=provider.execute_tool_call,
                max_iterations=MAX_SIMULATION_TURNS,
                max_prompt_tokens=SIMULATION_TOKEN_BUDGET,
            ),
            options=LLMCallOptions(
                run_id=state.get("run_id"),
                prompt_name=f"simulation_execution_{hypothesis.id}",
            ),
        )
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        # If the closing turn fails too, mental simulation still supplies a
        # complete review.
        logger.warning("simulation execution failed for %s: %s", hypothesis.id, exc)
        return ""
    return observations.strip()
