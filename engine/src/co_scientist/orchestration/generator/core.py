import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from co_scientist.core.constants import (
    DEFAULT_EVOLUTION_MAX_COUNT,
    DEFAULT_INITIAL_HYPOTHESES_COUNT,
    DEFAULT_MAX_ITERATIONS,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.orchestration.generator.initial_state import (
    RunCapabilities,
    RunIdentity,
    _build_initial_state,
)
from co_scientist.orchestration.generator.run_setup import (
    GeneratorOptions,
    _build_tool_registry,
    _resolve_dev_isolation_flag,
    _resolve_dev_mode_flag,
    _resolve_generation_strategy,
    _resolve_meta_review,
    _resolve_overview_review,
    _resolve_research_tier,
    _resolve_run_identity,
    _resolve_simulation_execution,
    _resolve_tool_calling_generation,
)

logger = logging.getLogger(__name__)


def _decide_enable_literature_review(opts: dict[str, Any], mcp_available: bool) -> bool:
    enable_literature_review_node = opts.get("enable_literature_review_node", mcp_available)
    if not mcp_available and enable_literature_review_node:
        logger.warning("Literature review node requested but MCP server unavailable - disabling")
        return False
    return cast(bool, enable_literature_review_node)


class HypothesisGenerator:
    _mcp_available: bool | None
    _pubmed_available: bool | None

    def __init__(
        self,
        model_name: str = "deepseek/deepseek-v4-flash",
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        initial_hypotheses_count: int = DEFAULT_INITIAL_HYPOTHESES_COUNT,
        evolution_max_count: int = DEFAULT_EVOLUTION_MAX_COUNT,
        options: GeneratorOptions | None = None,
    ):
        opts = options if options is not None else GeneratorOptions()
        from co_scientist.core.run_modes import validate_planning_config

        budget = {"max_iterations": max_iterations, **(opts.budget or {})}
        validate_planning_config(budget, "ultra")
        validate_planning_config(
            {
                "initial_hypotheses_count": initial_hypotheses_count,
                "evolution_max_count": evolution_max_count,
                "max_iterations": max_iterations,
                "k_factor": opts.elo_k_factor,
                "tournament_pairs": opts.tournament_pairs,
                "evidence_count": opts.literature_review_papers_count,
            },
            "ultra",
        )
        if opts.elo_k_factor <= 0:
            raise ValueError("elo_k_factor must be positive")
        self.model_name = model_name
        self.supervisor_model_name = opts.supervisor_model_name or model_name
        self.max_iterations = max_iterations
        self.budget = budget
        self.initial_hypotheses_count = initial_hypotheses_count
        self.evolution_max_count = evolution_max_count
        self.tournament_pairs = opts.tournament_pairs
        self.elo_k_factor = opts.elo_k_factor
        self.literature_review_papers_count = opts.literature_review_papers_count
        # Credentials stay outside checkpoints.
        self.api_key = opts.api_key
        # Keep the provider-neutral bundled registry unless custom configuration
        # replaces it.
        self._tool_registry = _build_tool_registry(opts.disable_tools)
        self.invalidate_configuration_caches()

    async def prepare_task_state(
        self,
        research_goal: str,
        *,
        progress_callback: None | (Callable[[str, dict[str, Any]], Awaitable[None]]) = None,
        opts: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> WorkflowState:
        start_time, run_id = _resolve_run_identity(run_id)
        opts = opts or {}
        user_inputs = opts.get("user_inputs") or {}
        capabilities = await self._resolve_generation_settings(opts)
        return _build_initial_state(
            config_fields=self._initial_config_fields(),
            identity=RunIdentity(
                research_goal=research_goal,
                start_time=start_time,
                run_id=run_id,
                progress_callback=progress_callback,
            ),
            capabilities=capabilities,
            opts=opts,
            user_inputs=user_inputs,
        )

    async def _resolve_generation_settings(self, opts: dict[str, Any]) -> RunCapabilities:
        (
            mcp_available,
            pubmed_available,
            enable_literature_review_node,
        ) = await self._resolve_literature_review_settings(opts)
        # Forced tool strategies must fail when tool calling is unavailable.
        enable_tool_calling = _resolve_tool_calling_generation(
            opts,
            mcp_available,
            enable_literature_review_node,
            self.model_name,
        )
        capabilities = RunCapabilities(
            mcp_available=mcp_available,
            pubmed_available=pubmed_available,
            enable_tool_calling_generation=enable_tool_calling,
            enable_simulation_execution=_resolve_simulation_execution(opts, self.model_name),
            enable_overview_review=_resolve_overview_review(opts, self.supervisor_model_name),
            research_tier=_resolve_research_tier(opts, mcp_available),
            enable_meta_review=_resolve_meta_review(opts),
            generation_strategy=_resolve_generation_strategy(opts, enable_tool_calling),
            dev_test_lit_tools_isolation=_resolve_dev_isolation_flag(opts),
            dev_mode=_resolve_dev_mode_flag(opts),
        )
        return capabilities

    def _initial_config_fields(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "supervisor_model_name": self.supervisor_model_name,
            "max_iterations": self.max_iterations,
            "initial_hypotheses_count": self.initial_hypotheses_count,
            "evolution_max_count": self.evolution_max_count,
            "tournament_pairs": self.tournament_pairs,
            "elo_k_factor": self.elo_k_factor,
            "literature_review_papers_count": (self.literature_review_papers_count),
            "budget": self.budget,
            "tool_registry": self._tool_registry,
        }

    @property
    def tool_registry(self) -> Any:
        return self._tool_registry

    async def _check_cached_availability(self) -> tuple[bool, bool]:
        # Availability flags are always set together, so knowing either
        # establishes both.
        if self._mcp_available is not None and self._pubmed_available is not None:
            return self._mcp_available, self._pubmed_available

        from co_scientist.platform.retrieval.mcp_client import (
            check_literature_source_available,
            check_mcp_available,
        )

        mcp_available, pubmed_available = await asyncio.gather(
            check_mcp_available(tool_registry=self._tool_registry),
            check_literature_source_available(tool_registry=self._tool_registry),
        )
        self._mcp_available = mcp_available
        self._pubmed_available = pubmed_available
        return mcp_available, pubmed_available

    async def _resolve_literature_review_settings(
        self,
        opts: dict[str, Any],
    ) -> tuple[bool, bool, bool]:
        if opts.get("enable_literature_review_node") is False:
            return False, False, False

        (
            mcp_available,
            pubmed_available,
        ) = await self._check_cached_availability()

        enable_literature_review_node = _decide_enable_literature_review(opts, mcp_available)
        return mcp_available, pubmed_available, enable_literature_review_node

    def invalidate_configuration_caches(self) -> None:
        self._mcp_available = None
        self._pubmed_available = None
