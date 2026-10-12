"""Resolve ambient flags at setup so nodes depend on checkpointed state rather
than environment.
"""

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from co_scientist.core.constants import ELO_K_FACTOR
from co_scientist.platform.llm.offline.llm import is_offline_model
from co_scientist.platform.retrieval.config.registry import parse_bool_env
from co_scientist.platform.retrieval.research_adapter import tier_researches
from co_scientist.platform.sandbox.workspace.tools import (
    command_execution_unavailable_reason,
)
from co_scientist.science.generation.operations import (
    GENERATION_STRATEGY_LABELS,
    TOOLS_REQUIRING_STRATEGIES,
)


@dataclass(frozen=True)
class GeneratorOptions:
    """Credentials stay outside checkpoints."""

    supervisor_model_name: str | None = None
    tournament_pairs: int = 12
    elo_k_factor: int = ELO_K_FACTOR
    literature_review_papers_count: int = 8
    disable_tools: list[str] | None = None
    budget: dict[str, Any] | None = field(default=None)
    api_key: str | None = None


logger = logging.getLogger(__name__)


def _resolve_meta_review(opts: dict[str, Any]) -> bool:
    """Disabling cadence removes periodic feedback, not the EVOLVE branch's
    meta-review node.
    """
    return opts.get("enable_meta_review", True) is not False


def _resolve_generation_strategy(opts: dict[str, Any], enable_tool_calling_generation: bool) -> str:
    """Forced tool strategies need a real tool loop; unavailable prerequisites
    must fail.
    """
    requested = opts.get("generation_strategy")
    if not isinstance(requested, str) or requested not in (GENERATION_STRATEGY_LABELS):
        return ""
    if requested in TOOLS_REQUIRING_STRATEGIES and not enable_tool_calling_generation:
        logger.warning(
            "generation_strategy=%s requires tool-calling generation, which"
            " is off for this run - deriving the strategy instead",
            requested,
        )
        return ""
    return requested


def _resolve_tool_calling_generation(
    opts: dict[str, Any],
    mcp_available: bool,
    enable_literature_review_node: bool,
    model_name: str,
) -> bool:
    """Tool loops re-buy prior results each turn; only explicitly funded
    callers opt in. Offline responders never emit tool calls.
    """
    requested = opts.get("enable_tool_calling_generation")

    if _tool_calling_disabled_before_availability(requested, model_name):
        return False

    if not mcp_available:
        if requested:
            logger.warning(
                "enable_tool_calling_generation=True but MCP server"
                " unavailable - disabling tool-calling mode"
            )
        return False

    return _resolve_tool_calling_given_mcp_available(opts, enable_literature_review_node, requested)


def _resolve_simulation_execution(opts: dict[str, Any], model_name: str) -> bool:
    """Confinement availability belongs to the executing worker and is checked
    at point of use.
    """
    requested = opts.get("enable_simulation_execution")
    if not requested:
        return False
    if is_offline_model(model_name):
        logger.warning(
            "enable_simulation_execution=True but the offline backend is "
            "active - the simulation review will step through mentally"
        )
        return False
    unavailable = command_execution_unavailable_reason()
    if unavailable is not None:
        logger.warning(
            "enable_simulation_execution=True but command execution is "
            "unavailable (%s) - the simulation review will step through mentally",
            unavailable,
        )
        return False
    return True


def _resolve_overview_review(opts: dict[str, Any], model_name: str) -> bool:
    """Offline schema responses are deterministic; accuracy review would check
    nothing real.
    """
    requested = opts.get("enable_overview_review")
    if not requested:
        return False
    if is_offline_model(model_name):
        logger.warning(
            "enable_overview_review=True but the offline backend is "
            "active - the research overview will publish unreviewed"
        )
        return False
    return True


def _resolve_research_tier(opts: dict[str, Any], mcp_available: bool) -> str:
    """Adapter-owned budgets serve reviews even without the literature node.
    Offline schema calls exercise research, unlike tool loops.
    """
    requested = opts.get("research_tier")
    if not isinstance(requested, str) or not tier_researches(requested):
        return ""
    if not mcp_available:
        logger.warning(
            "research_tier=%s requested but no literature tools are"
            " available - skipping deep research",
            requested,
        )
        return ""
    return requested


def _tool_calling_disabled_before_availability(requested: bool | None, model_name: str) -> bool:
    """Offline responders never emit tool calls; explicit opt-outs avoid
    availability probes.
    """
    if is_offline_model(model_name):
        if requested:
            logger.warning(
                "enable_tool_calling_generation=True but the offline"
                " backend is active - disabling tool-calling mode"
            )
        return True

    if requested is False:
        logger.info("Tool-calling generation disabled by caller option")
        return True

    return False


def _resolve_tool_calling_given_mcp_available(
    opts: dict[str, Any],
    enable_literature_review_node: bool,
    requested: bool | None,
) -> bool:
    if enable_literature_review_node:
        # Tool loops re-buy their growing transcript each turn; caller opt-in
        # funds this cost.
        return requested is True

    if requested and opts.get("enable_literature_review_node") is False:
        raise ValueError(
            "enable_tool_calling_generation requires"
            " enable_literature_review_node=True. "
            "Tool-calling generation needs literature context"
            " from the review node."
        )

    if requested:
        logger.warning(
            "enable_tool_calling_generation=True but literature"
            " review node unavailable - disabling tool-calling mode"
        )
    return False


def _resolve_run_identity(run_id: str | None) -> tuple[float, str]:
    start_time = time.time()
    if run_id is None:
        run_id = str(uuid.uuid4())
    logger.info("Starting hypothesis generation with run_id=%s", run_id)
    return start_time, run_id


def _resolve_dev_isolation_flag(opts: dict[str, Any]) -> bool:
    enabled = bool(opts.get("dev_test_lit_tools_isolation", False))
    if enabled:
        logger.info("Dev isolation mode enabled: all hypotheses use lit tools")
    return enabled


def _resolve_dev_mode_flag(opts: dict[str, Any]) -> bool:
    """Resolve the environment default into checkpoint state; explicit per-run
    options win.
    """
    requested = opts.get("dev_mode")
    if requested is None:
        requested = parse_bool_env(os.getenv("COSCIENTIST_DEV_MODE", "false"))
    enabled = bool(requested)
    if enabled:
        logger.info("Dev mode enabled: using the reduced literature-review budget")
    return enabled


def _build_tool_registry(disable_tools: list[str] | None) -> Any:
    from co_scientist.platform.retrieval.config import (
        ToolRegistry,
    )

    registry = ToolRegistry(disabled_tools=disable_tools)
    logger.info("Initialized tool registry: %s enabled tools", len(registry.get_enabled_tools()))
    return registry
