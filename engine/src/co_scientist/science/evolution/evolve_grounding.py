import logging

from co_scientist.core.constants import (
    DEFAULT_MAX_TOKENS,
    LITERATURE_REVIEW_MAX_QUERIES,
    LOW_TEMPERATURE,
)
from co_scientist.core.exceptions import TASK_CONTROL_FLOW_ERRORS
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import CompletionSpec, LLMCallOptions, call_llm_json
from co_scientist.platform.retrieval.article import Article
from co_scientist.science.evidence_context import (
    RETRIEVED_LABEL,
    EvidenceCaps,
    _retrieve_probe_evidence,
    build_evidence_context,
)
from co_scientist.science.prompts import get_hypothesis_query_generation_prompt
from co_scientist.science.schemas import LITERATURE_QUERY_SCHEMA

logger = logging.getLogger(__name__)

_MAX_GROUNDING_QUERIES = LITERATURE_REVIEW_MAX_QUERIES
_MAX_GROUNDING_ARTICLES = 6

_NO_EVIDENCE_TEXT = "No retrieved evidence is available to ground this refinement."

_NOT_APPLICABLE_TEXT = (
    "Targeted literature retrieval applies to the enhancement operator "
    "only; this operator works from the context already provided."
)


def not_applicable_block() -> str:
    return _NOT_APPLICABLE_TEXT


async def enhancement_grounding_block(state: WorkflowState, hypothesis: Hypothesis) -> str:
    """Empty or failed live retrieval falls back to the accumulated corpus,
    so optional grounding cannot prevent refinement."""
    articles = await _retrieve_parent_evidence(state, hypothesis)
    if articles:
        return build_evidence_context(
            articles,
            require_analyzed=False,
            article_label=RETRIEVED_LABEL,
            caps=EvidenceCaps(articles=_MAX_GROUNDING_ARTICLES),
        )
    run_articles = state.get("articles") or []
    if run_articles:
        # Both analyzed corpus and targeted reflection sources can ground
        # enhancement.
        return build_evidence_context(
            run_articles,
            require_analyzed=False,
            article_label=RETRIEVED_LABEL,
            caps=EvidenceCaps(articles=_MAX_GROUNDING_ARTICLES),
        )
    return _NO_EVIDENCE_TEXT


async def _retrieve_parent_evidence(state: WorkflowState, hypothesis: Hypothesis) -> list[Article]:
    if not state.get("mcp_available"):
        return []
    queries = await _parent_search_queries(state, hypothesis)
    if not queries:
        return []
    articles, errors = await _retrieve_probe_evidence(state, queries)
    if errors:
        logger.warning(
            "Enhancement grounding retrieval degraded for %s: %s",
            hypothesis.id,
            errors,
        )
    return articles


async def _parent_search_queries(state: WorkflowState, hypothesis: Hypothesis) -> list[str]:
    """Keyword backends AND terms; prose searches would match nothing."""
    try:
        result = await call_llm_json(
            prompt=get_hypothesis_query_generation_prompt(
                research_goal=state["research_goal"],
                hypothesis=hypothesis.text,
            ),
            spec=CompletionSpec(
                role="grounding_queries",
                model_name=state["model_name"],
                max_tokens=DEFAULT_MAX_TOKENS,
                temperature=LOW_TEMPERATURE,
                json_schema=LITERATURE_QUERY_SCHEMA,
            ),
            options=LLMCallOptions(
                run_id=state.get("run_id"),
                prompt_name=f"evolution_grounding_queries_{hypothesis.id}",
            ),
        )
    except TASK_CONTROL_FLOW_ERRORS:
        raise
    except Exception as exc:
        logger.warning(
            "Enhancement query generation failed for %s: %s",
            hypothesis.id,
            exc,
        )
        return []
    queries = [
        " ".join(str(query).split()) for query in result.get("queries") or [] if str(query).strip()
    ]
    return queries[:_MAX_GROUNDING_QUERIES]


def counts_query_call(state: WorkflowState, operator_value: str) -> bool:
    return bool(state.get("mcp_available")) and operator_value == "enhancement"


def grounding_metrics_extra(state: WorkflowState, operator_values: list[str]) -> int:
    """MCP retrieval is not model spend; count query formulation only when
    the enhancement actually attempts it."""
    return sum(1 for value in operator_values if counts_query_call(state, value))
