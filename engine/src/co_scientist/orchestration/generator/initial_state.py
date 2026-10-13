from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from co_scientist.domains.research_state.models import ExecutionMetrics
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.degradation import (
    resolve_retrieval_degradation,
)

ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None]] | None


@dataclass(frozen=True)
class RunIdentity:
    research_goal: str
    start_time: float
    run_id: str
    progress_callback: ProgressCallback = None


@dataclass(frozen=True)
class RunCapabilities:
    mcp_available: bool = False
    pubmed_available: bool = False
    enable_tool_calling_generation: bool = False
    enable_simulation_execution: bool = False
    enable_overview_review: bool = False
    dev_test_lit_tools_isolation: bool = False
    dev_mode: bool = False
    research_tier: str = ""
    enable_meta_review: bool = True
    generation_strategy: str = ""


def _initial_runtime_fields() -> dict[str, Any]:
    return {
        "hypotheses": [],
        "current_iteration": 0,
        "task_history": [],
        "next_task": None,
        "termination_reason": None,
        # Generator configuration supplies budget, not initial state
        # construction.
        "orchestrator_state": {},
        "supervisor_guidance": {},
        "meta_review": {},
        "research_overview": None,
        "removed_duplicates": [],
        "proximity_graph": {},
        "tournament_matchups": [],
        "evolution_details": [],
        # Audit/manual-review lists must survive checkpoint restoration.
        "safety_decisions": [],
        "held_for_review": [],
        # Reports must distinguish degradation from genuine empty results.
        "degraded_nodes": [],
        "metrics": ExecutionMetrics(),
        "messages": [],
    }


def _initial_run_identity_fields(
    identity: RunIdentity, capabilities: RunCapabilities
) -> dict[str, Any]:
    return {
        "research_goal": identity.research_goal,
        "start_time": identity.start_time,
        "run_id": identity.run_id,
        "progress_callback": identity.progress_callback,
        "mcp_available": capabilities.mcp_available,
        "pubmed_available": capabilities.pubmed_available,
        "enable_tool_calling_generation": (capabilities.enable_tool_calling_generation),
        "enable_simulation_execution": (capabilities.enable_simulation_execution),
        "dev_test_lit_tools_isolation": (capabilities.dev_test_lit_tools_isolation),
        "dev_mode": capabilities.dev_mode,
        "enable_overview_review": capabilities.enable_overview_review,
        "research_tier": capabilities.research_tier,
        "enable_meta_review": capabilities.enable_meta_review,
        "generation_strategy": capabilities.generation_strategy,
    }


def _initial_user_and_literature_fields(
    *,
    opts: dict[str, Any],
    user_inputs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "preferences": opts.get("preferences"),
        # High-priority steering waits for the next safe boundary before
        # consumption.
        "pending_steering": bool(opts.get("pending_steering")),
        "attributes": opts.get("attributes"),
        "constraints": opts.get("constraints"),
        # Goal-interview lab constraints feed generation and evolution
        # feasibility prompts.
        "lab_constraints": opts.get("lab_constraints"),
        "criteria": opts.get("criteria"),
        "run_focus_guidance": opts.get("run_focus_guidance"),
        "run_setup_guidance": opts.get("run_setup_guidance"),
        "starting_hypotheses": user_inputs.get("starting_hypotheses"),
        "literature": user_inputs.get("literature"),
        "articles_with_reasoning": None,
        "literature_review_queries": None,
        "articles": None,
        "debate_transcripts": None,
        "context_enrichment_sources": opts.get("context_enrichment_sources"),
    }


def _build_initial_state(
    *,
    config_fields: dict[str, Any],
    identity: RunIdentity,
    capabilities: RunCapabilities,
    opts: dict[str, Any],
    user_inputs: dict[str, Any],
) -> WorkflowState:
    return cast(
        WorkflowState,
        {
            **config_fields,
            **_initial_runtime_fields(),
            **_initial_run_identity_fields(identity, capabilities),
            **_initial_user_and_literature_fields(opts=opts, user_inputs=user_inputs),
            "retrieval_degradation": resolve_retrieval_degradation(
                mcp_available=bool(capabilities.mcp_available),
                private_sources=opts.get("context_enrichment_sources"),
            ),
        },
    )
