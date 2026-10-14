import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from co_scientist.core.constants import (
    DEFAULT_MAX_TOKENS,
    HIGH_TEMPERATURE,
    LITERATURE_REVIEW_MAX_QUERIES,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    call_llm_json,
)
from co_scientist.platform.retrieval.evidence.retrieval_support import (
    describe_exception,
)
from co_scientist.platform.retrieval.evidence.search_support import (
    SearchConfig,
    determine_query_source_type,
    parse_mcp_query_result,
)
from co_scientist.platform.retrieval.mcp_client import MCPToolClient
from co_scientist.science.prompts import (
    LiteratureQueryInputs,
    get_literature_review_query_generation_prompt,
)
from co_scientist.science.schemas import LITERATURE_QUERY_SCHEMA

if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import WorkflowConfig

logger = logging.getLogger(__name__)


async def _generate_queries_via_mcp(
    mcp_client: MCPToolClient,
    research_goal: str,
    tool_name: str,
    query_format: str,
) -> list[str]:
    try:
        result = await mcp_client.call_tool(
            tool_name,
            research_goal=research_goal,
            query_format=query_format,
        )
        queries = parse_mcp_query_result(result)
        logger.info("MCP query generation returned %s queries", len(queries))
        return queries
    except Exception as e:
        # An empty MCP result signals LLM fallback, not the end of review.

        logger.warning(
            "MCP query generation failed: %s, falling back to LLM",
            describe_exception(e),
        )
        return []


def _build_query_generation_prompt(
    state: WorkflowState,
    config: SearchConfig,
) -> str:
    source_type = determine_query_source_type(
        config.workflow,
        config.tool_registry,
        config.search_tool_config,
        config.is_multi_source,
    )
    logger.debug("Using %s query generation prompt", source_type)
    return get_literature_review_query_generation_prompt(
        research_goal=state["research_goal"],
        source_type=source_type,
        inputs=LiteratureQueryInputs(
            preferences=state.get("preferences", ""),
            attributes=state.get("attributes", []),
            user_literature=state.get("literature", []),
            user_hypotheses=state.get("starting_hypotheses", []),
        ),
        # The critique identifies missing or weak evidence; absent on the first
        # cycle.
        meta_review=state.get("meta_review"),
    )


async def _generate_queries_via_llm(
    state: WorkflowState,
    config: SearchConfig,
) -> list[str]:
    prompt = _build_query_generation_prompt(state, config)
    try:
        result = await call_llm_json(
            prompt=prompt,
            spec=CompletionSpec(
                role="literature_queries",
                model_name=state["model_name"],
                max_tokens=DEFAULT_MAX_TOKENS,
                temperature=HIGH_TEMPERATURE,
                json_schema=LITERATURE_QUERY_SCHEMA,
            ),
        )
        return cast(list[str], result.get("queries", []))
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as e:
        logger.warning("LLM query generation failed: %s", e)
        return []


def _resolve_query_format(workflow: "WorkflowConfig") -> str:
    return workflow.query_format or "boolean"


def _resolve_query_generation_tool(
    config: SearchConfig,
) -> tuple[str, str] | None:
    if not (config.tool_registry and config.workflow and config.workflow.query_generation_tool):
        return None

    tool_cfg = config.tool_registry.get_tool(config.workflow.query_generation_tool)
    if not tool_cfg:
        return None

    return tool_cfg.mcp_tool_name, _resolve_query_format(config.workflow)


async def _try_mcp_query_generation(
    state: WorkflowState,
    config: SearchConfig,
    mcp_client: MCPToolClient,
) -> list[str]:
    resolved = _resolve_query_generation_tool(config)
    if not resolved:
        return []

    tool_name, query_format = resolved
    logger.info("Using MCP query generation: %s (format: %s)", tool_name, query_format)
    return await _generate_queries_via_mcp(
        mcp_client,
        state["research_goal"],
        tool_name,
        query_format,
    )


_GOAL_FALLBACK_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "for",
        "in",
        "on",
        "to",
        "and",
        "or",
        "not",
        "is",
        "are",
        "how",
        "does",
        "do",
        "what",
        "why",
        "can",
        "could",
        "will",
        "would",
        "that",
        "this",
        "these",
        "those",
        "by",
        "as",
        "be",
        "using",
        "use",
        "via",
        "into",
        "from",
        "about",
        "we",
        "our",
        "with",
    }
)


_GOAL_FALLBACK_MAX_TERMS = 8


def _distill_goal_to_query(research_goal: str) -> str:
    """PubMed ANDs every term; prose goals collapse recall, so the
    no-generator fallback must be keyword-shaped."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", research_goal)
    keywords = [w for w in words if w.lower() not in _GOAL_FALLBACK_STOPWORDS]
    if not keywords:
        return research_goal
    return " ".join(keywords[:_GOAL_FALLBACK_MAX_TERMS])


@dataclass(frozen=True)
class QueryPhaseResult:
    queries: list[str]
    llm_calls: int


async def _phase1_generate_queries(
    state: WorkflowState,
    config: SearchConfig,
    mcp_client: MCPToolClient,
) -> QueryPhaseResult:
    logger.info("Phase 1: generating search queries")

    queries = await _try_mcp_query_generation(state, config, mcp_client)

    llm_calls = 0
    if not queries:
        queries = await _generate_queries_via_llm(state, config)
        llm_calls = 1

    # Even when both generators fail, retrieval still needs a query.

    if not queries:
        fallback = _distill_goal_to_query(state["research_goal"])
        logger.warning(
            "No queries generated, falling back to distilled goal: %s",
            fallback,
        )
        queries = [fallback]

    # Cap query fan-out regardless of how many queries a generator returns.

    queries = queries[:LITERATURE_REVIEW_MAX_QUERIES]

    logger.info("Generated %s search queries", len(queries))
    for i, q in enumerate(queries, 1):
        logger.debug("Query %s: %s", i, q)

    return QueryPhaseResult(queries=queries, llm_calls=llm_calls)
