import logging
from typing import Any, NamedTuple

from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.evidence.article_support import records_from_findings
from co_scientist.platform.retrieval.evidence.search_support import (
    SearchConfig,
)
from co_scientist.platform.retrieval.mcp_client import MCPToolClient
from co_scientist.platform.retrieval.research import (
    ResearchBudget,
    ResearchResult,
    conduct_research,
    result_to_dict,
)
from co_scientist.platform.retrieval.research_adapter import (
    McpRetrieval,
    ResearchRun,
    budget_for_tier,
)
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.research_model import LlmResearchModel

logger = logging.getLogger(__name__)


_GAP_FIELDS = ("gaps_identified", "unexplored_areas", "methodology_limitations")

# Findings stay whole in the ledger; bound what downstream generation prompts
# must carry.


_MAX_LISTED_FINDINGS = 40


class ResearchOutcome(NamedTuple):
    ledger: dict[str, Any]
    records: dict[str, dict[str, Any]]
    section: str


async def run_research_phase(
    state: WorkflowState,
    config: SearchConfig,
    mcp_client: MCPToolClient,
    analyses: list[dict[str, Any]],
) -> ResearchOutcome | None:
    prepared = _prepare(state, config, mcp_client)
    if prepared is None:
        return None
    retrieval, budget = prepared

    await emit_progress(
        state,
        "literature_research_start",
        f"Researching open questions ({budget.max_threads()} threads max)...",
        0.6,
    )
    result = await conduct_research(
        goal=config.research_goal,
        model=LlmResearchModel(config.model_name, run_id=str(state.get("run_id") or "")),
        retrieval=retrieval,
        budget=budget,
        seed_questions=_seed_questions(analyses, budget.breadth),
    )
    logger.info(
        "Deep research: %s threads, %s calls, %s findings (%s)",
        len(result.threads),
        len(result.calls),
        len(result.findings),
        result.stop_reason.value,
    )
    return ResearchOutcome(
        ledger=result_to_dict(result),
        records=records_from_findings(result, retrieval),
        section=_synthesis_section(result),
    )


def _prepare(
    state: WorkflowState, config: SearchConfig, mcp_client: MCPToolClient
) -> tuple[McpRetrieval, ResearchBudget] | None:
    tier = str(state.get("research_tier") or "")
    if not tier or config.workflow is None or config.tool_registry is None:
        return None
    retrieval = McpRetrieval(
        mcp_client,
        config.tool_registry,
        config.workflow,
        ResearchRun(
            run_id=str(state.get("run_id") or ""),
            research_goal=config.research_goal,
        ),
    )
    budget = budget_for_tier(tier, retrieval.sources)
    return None if budget is None else (retrieval, budget)


def _seed_questions(analyses: list[dict[str, Any]], limit: int) -> list[str]:
    """Paper-identified gaps earn the first research questions without
    another planning call; plan from the goal only when no gaps exist."""
    seeds: list[str] = []
    seen: set[str] = set()
    for entry in analyses:
        for gap in _gaps_in(entry.get("analysis")):
            if gap.lower() in seen:
                continue
            seen.add(gap.lower())
            seeds.append(gap)
            if len(seeds) >= limit:
                return seeds
    return seeds


def _gaps_in(analysis: object) -> list[str]:
    if not isinstance(analysis, dict):
        return []
    stated = (analysis.get(name) for name in _GAP_FIELDS)
    return [value.strip() for value in stated if isinstance(value, str) and value.strip()]


def _synthesis_section(result: ResearchResult) -> str:
    """Later agents read synthesis; findings confined to the ledger would be
    recorded but never used."""
    if not result.findings:
        return ""
    lines = [
        "",
        "## Open questions researched",
        "",
        (
            f"Researched {len(result.threads)} question(s) over"
            f" {result.levels_run} level(s), following what each reading"
            f" left open. Stopped: {result.stop_reason.value}."
        ),
        "",
    ]
    for summary in result.summaries():
        lines.extend([summary, ""])
    lines.extend(["### Findings", ""])
    for finding in result.findings[:_MAX_LISTED_FINDINGS]:
        lines.append(f"- {finding.text} [{finding.locator}]")
    declined = result.declined()
    if declined:
        lines.extend(
            [
                "",
                (
                    f"{len(declined)} further question(s) were raised and"
                    " not researched, for budget."
                ),
            ]
        )
    return "\n".join(lines)
