from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, cast

from co_scientist.core.constants import (
    LITERATURE_REVIEW_PAPERS_COUNT,
    LITERATURE_REVIEW_PAPERS_COUNT_DEV,
)
from co_scientist.platform.retrieval.evidence.search_fusion import (
    merge_search_results as merge_search_results,
)

if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import (
        ToolConfig,
        ToolRegistry,
        WorkflowConfig,
    )

logger = logging.getLogger(__name__)


@dataclass
class SearchConfig:
    tool_registry: ToolRegistry | None
    workflow: WorkflowConfig | None
    is_multi_source: bool
    search_tool_name: str
    search_tool_config: ToolConfig | None
    source_name: str
    papers_to_read_count: int
    is_dev_mode: bool

    research_goal: str = ""
    model_name: str = ""

    # Probe retrieval runs per hypothesis; repeated semantic batching multiplies
    # cost. Opting out preserves lexical ranking and result count.
    semantic_relevance_enabled: bool = True


def _primary_search_tool(
    tool_registry: ToolRegistry | None,
    workflow: WorkflowConfig | None,
    is_multi_source: bool,
) -> ToolConfig | None:
    if is_multi_source and workflow is not None:
        sources = workflow.get_enabled_search_sources()
        logger.info(
            "Multi-source mode: %s sources configured: %s",
            len(sources),
            [source.tool for source in sources],
        )
        return None
    if tool_registry and workflow and workflow.primary_search:
        return tool_registry.get_tool(workflow.primary_search)
    return None


# The WorkflowState keys search reads; retrieval sits below the domain state,
# which satisfies this structurally.
class SearchState(TypedDict):
    research_goal: str
    run_id: str
    model_name: str
    literature_review_papers_count: int
    dev_mode: bool | None
    tool_registry: Any | None


def search_config_for(state: SearchState) -> SearchConfig:
    tool_registry = state.get("tool_registry")
    workflow = tool_registry.get_workflow("literature_review") if tool_registry else None
    is_multi_source = bool(workflow and workflow.is_multi_source())
    tool = _primary_search_tool(tool_registry, workflow, is_multi_source)
    source_name = extract_source_name(tool) if tool else "pubmed"
    search_tool_name = tool.mcp_tool_name if tool else "pubmed_search_with_fulltext"
    if tool:
        logger.info(
            "Single-source mode: %s (source: %s)",
            search_tool_name,
            source_name,
        )
    is_dev_mode = bool(state.get("dev_mode", False))
    return SearchConfig(
        tool_registry=tool_registry,
        workflow=workflow,
        is_multi_source=is_multi_source,
        search_tool_name=search_tool_name,
        search_tool_config=tool,
        source_name=source_name,
        papers_to_read_count=(
            LITERATURE_REVIEW_PAPERS_COUNT_DEV
            if is_dev_mode
            else int(state.get("literature_review_papers_count") or LITERATURE_REVIEW_PAPERS_COUNT)
        ),
        is_dev_mode=is_dev_mode,
        research_goal=str(state.get("research_goal") or ""),
        model_name=str(state.get("model_name") or ""),
    )


def _quoted_field_mapping_source(tool_config: ToolConfig) -> str | None:
    """YAML field_mapping encodes static source labels as quoted literals,
    not field names."""
    if not (tool_config.response_format and tool_config.response_format.field_mapping):
        return None
    source_val = tool_config.response_format.field_mapping.get("source", "")
    if not (source_val.startswith("'") and source_val.endswith("'")):
        return None
    return source_val[1:-1]


def extract_source_name(tool_config: ToolConfig | None) -> str:
    if not tool_config:
        return "unknown"
    literal_source = _quoted_field_mapping_source(tool_config)
    if literal_source is not None:
        return literal_source

    return tool_config.source_type or "unknown"


def _resolve_source_id_field(tool_config: ToolConfig) -> str:
    """The @ prefix is field-mapping transform syntax, not a native response
    key."""
    source_id_field = tool_config.response_format.field_mapping.get("source_id", "source_id")
    if source_id_field.startswith("@"):
        return "arxiv_id"
    return source_id_field


def _rekey_list_response_by_id(
    papers: list[Any],
    tool_config: ToolConfig,
) -> dict[str, Any]:
    source_id_field = _resolve_source_id_field(tool_config)
    normalized: dict[str, Any] = {}
    for paper in papers:
        paper_id = cast(
            str,
            paper.get(source_id_field)
            or paper.get("arxiv_id")
            or paper.get("id")
            or str(len(normalized)),
        )
        normalized[paper_id] = paper
    return normalized


def _extract_results_path(result_data: Any, results_path: str | None) -> Any:
    if results_path and results_path != "." and isinstance(result_data, dict):
        return result_data.get(results_path, result_data)
    return result_data


def _normalize_with_response_format(
    result_data: Any,
    tool_config: ToolConfig,
) -> dict[str, dict[str, Any]]:
    response_format = tool_config.response_format
    result_data = _extract_results_path(result_data, response_format.results_path)

    if response_format.is_dict and isinstance(result_data, dict):
        return result_data

    if isinstance(result_data, list):
        return _rekey_list_response_by_id(result_data, tool_config)

    return result_data if isinstance(result_data, dict) else {}


def normalize_search_response(
    result_data: Any,
    tool_config: ToolConfig | None,
) -> dict[str, dict[str, Any]]:
    if isinstance(result_data, dict) and result_data.get("status") == "ok":
        return {
            str(record.get("source_id") or record.get("id") or record.get("url") or index): record
            for index, record in enumerate(result_data["records"])
        }
    if not isinstance(result_data, (dict, list)):
        return {}

    if not tool_config or not tool_config.response_format:
        return result_data if isinstance(result_data, dict) else {}

    return _normalize_with_response_format(result_data, tool_config)


def parse_mcp_query_result(result: Any) -> list[str]:
    if isinstance(result, str):
        try:
            result_data = json.loads(result)

            if isinstance(result_data, list):
                return result_data
            return cast(list[str], result_data.get("queries", []))
        except json.JSONDecodeError:
            return []
    elif isinstance(result, list):
        return result
    return []


def determine_query_source_type(
    workflow: WorkflowConfig | None,
    tool_registry: ToolRegistry | None,
    search_tool_config: ToolConfig | None,
    is_multi_source: bool,
) -> str:
    # One specialized query prompt cannot serve every source of a multi-source run.
    if is_multi_source and workflow and tool_registry:
        return "academic"

    if search_tool_config:
        return search_tool_config.source_type or "academic"

    return "academic"
