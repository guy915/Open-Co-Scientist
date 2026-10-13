from __future__ import annotations

import logging
from typing import Any, NamedTuple

from co_scientist.core.constants import truncate
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.evidence.article_support import (
    build_articles_from_metadata,
    records_from_findings,
)
from co_scientist.platform.retrieval.research import (
    ResearchBudget,
    ResearchResult,
    conduct_research,
    result_to_dict,
)
from co_scientist.platform.retrieval.research_adapter import McpRetrieval, budget_for_tier
from co_scientist.platform.telemetry.progress import emit_progress
from co_scientist.science.research_model import LlmResearchModel

logger = logging.getLogger(__name__)


# A late-cycle pool can be large; bound explored-history context.


EXPANSION_POOL_SAMPLE_SIZE = 12


EXPANSION_POOL_ITEM_CHARS = 200


# Broad exploratory retrieval searches and reads more than focused drafting.

EXPANSION_EXTRA_DRAFT_ITERATIONS = 2


def is_research_expansion(state: WorkflowState) -> bool:
    return int(state.get("current_iteration") or 0) > 0


def explored_hypothesis_summaries(
    state: WorkflowState,
) -> list[str]:
    hypotheses = state.get("hypotheses") or []
    summaries = [
        truncate(str(h.text).strip(), EXPANSION_POOL_ITEM_CHARS)
        for h in hypotheses[:EXPANSION_POOL_SAMPLE_SIZE]
    ]
    return [s for s in summaries if s]


def _explored_coverage_block(summaries: list[str]) -> str:
    if not summaries:
        return ""
    bullets = "".join(f"- {summary}\n" for summary in summaries)
    return (
        "Hypotheses already explored in this run (do NOT re-derive these"
        " directions; expand beyond them):\n"
        f"{bullets}\n"
    )


def _widened_evidence_block(state: WorkflowState) -> str:
    findings = str(state.get("research_expansion_findings") or "").strip()
    return f"\n{findings}\n" if findings else ""


def build_expansion_section(state: WorkflowState) -> str:
    if not is_research_expansion(state):
        return ""
    logger.info(
        "Research expansion cycle: switching generation to broad"
        " exploratory retrieval (iteration %s)",
        state.get("current_iteration"),
    )
    coverage = _explored_coverage_block(explored_hypothesis_summaries(state))
    widened = _widened_evidence_block(state)
    return (
        "## Research Expansion Cycle (broad exploratory retrieval)\n\n"
        "This is NOT the initial focused drafting pass. The goal of this"
        " cycle is to widen the evidence base before ideation:\n"
        "1. Run several DIVERSE searches first, before drafting anything:"
        " adjacent subtopics, different model systems or populations,"
        " alternative methodologies, and neighboring research areas"
        " relevant to the goal.\n"
        "2. Prefer sources and angles not already covered by the"
        " literature review context above.\n"
        "3. Only after this broader retrieval, draft hypotheses grounded"
        " in the newly widened evidence, targeting regions this run has"
        " not explored yet.\n\n"
        f"{coverage}"
        f"{widened}"
    )


# Widened findings must not displace the review synthesis in downstream prompts.


_MAX_LISTED_FINDINGS = 30


class ExpansionResearch(NamedTuple):
    section: str
    articles: list[Any]
    ledger: dict[str, Any]

    def applied_to(self, state: WorkflowState) -> WorkflowState:
        """Merge research articles before generation so the shared citation
        namespace lets strategies cite the newly found evidence."""
        merged: dict[str, Any] = {
            **state,
            "articles": [*(state.get("articles") or []), *self.articles],
            "research_expansion_findings": self.section,
        }
        return merged  # type: ignore[return-value]


async def research_for_expansion(
    state: WorkflowState,
) -> ExpansionResearch | None:
    prepared = await _prepare(state)
    if prepared is None:
        return None
    retrieval, budget = prepared

    await emit_progress(
        state,
        "research_expansion_start",
        f"Exploring beyond the ideas so far ({budget.max_threads()} max)...",
        0.1,
    )
    result = await _explore(state, retrieval, budget)
    logger.info(
        "Research expansion (iteration %s): %s threads, %s calls, %s findings (%s)",
        state.get("current_iteration"),
        len(result.threads),
        len(result.calls),
        len(result.findings),
        result.stop_reason.value,
    )
    if not result.findings:
        return None
    records = records_from_findings(result, retrieval)
    return ExpansionResearch(
        section=_expansion_section(result),
        articles=build_articles_from_metadata(records, ""),
        ledger=result_to_dict(result),
    )


async def _explore(
    state: WorkflowState,
    retrieval: McpRetrieval,
    budget: ResearchBudget,
) -> ResearchResult:
    return await conduct_research(
        goal=_expansion_goal(state),
        model=LlmResearchModel(
            str(state.get("model_name") or ""),
            run_id=str(state.get("run_id") or ""),
        ),
        retrieval=retrieval,
        budget=budget,
        seed_questions=[],
    )


async def _prepare(
    state: WorkflowState,
) -> tuple[McpRetrieval, ResearchBudget] | None:
    """Check every research gate before opening a client so unfunded
    exploration spends nothing."""
    from co_scientist.platform.retrieval.evidence.search_support import (
        search_config_for,
    )

    tier = str(state.get("research_tier") or "")
    if not is_research_expansion(state) or not tier:
        return None
    if not state.get("mcp_available"):
        return None
    config = search_config_for(state)
    if config.workflow is None or config.tool_registry is None:
        return None
    retrieval = await McpRetrieval.open_for(config, str(state.get("run_id") or ""))
    budget = budget_for_tier(tier, retrieval.sources)
    return None if budget is None else (retrieval, budget)


def _expansion_goal(state: WorkflowState) -> str:
    """Explored ideas constrain the planning goal rather than seed research;
    they mark territory to avoid, not questions to answer."""
    goal = str(state.get("research_goal") or "").strip()
    explored = explored_hypothesis_summaries(state)
    if not explored:
        return goal
    listed = "\n".join(f"- {summary}" for summary in explored)
    return (
        f"{goal}\n\nThis run has already explored the directions below."
        " Plan coverage of adjacent subtopics, alternative mechanisms,"
        " different model systems or populations, and neighbouring"
        " research areas that these do NOT cover.\n"
        f"{listed}"
    )


def _expansion_section(result: ResearchResult) -> str:
    lines = [
        "Evidence found by exploring beyond the ideas above"
        f" ({len(result.threads)} question(s) over {result.levels_run}"
        f" level(s); stopped: {result.stop_reason.value}). Ground new"
        " hypotheses in this material:",
        "",
    ]
    lines.extend(
        f"- {finding.text} [{finding.locator}]"
        for finding in result.findings[:_MAX_LISTED_FINDINGS]
    )
    return "\n".join(lines) + "\n"
