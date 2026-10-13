import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from co_scientist.domains.research_state.entity_names import extract_entity_names
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.evidence.retrieval_support import (
    reported_failure,
)
from co_scientist.platform.retrieval.evidence.search_support import (
    SearchConfig,
)
from co_scientist.platform.retrieval.mcp_client import MCPToolClient

if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import ToolConfig, ToolRegistry, WorkflowConfig

logger = logging.getLogger(__name__)


_CONTEXT_ENRICHMENT_MAX_CHARS = 1500

_CONTEXT_ENRICHMENT_RESULTS_PER_ENTITY = 4
# A decoded JSON null must remain distinct from failure to decode JSON.

_NOT_JSON = object()


async def _call_enrichment_tool_for_entity(
    tool_name: str,
    mapped_params: dict[str, Any],
    mcp_client: MCPToolClient,
) -> Any:
    try:
        return await mcp_client.call_tool(tool_name, **mapped_params)
    except Exception as e:
        # Optional background lookup failures skip that evidence rather than
        # abort review.

        logger.debug("context enrichment call failed (%s): %s", tool_name, e)
        return None


def _format_generic_items(
    items: list[Any],
) -> tuple[str, list[dict[str, Any]]]:
    capped = items[:_CONTEXT_ENRICHMENT_RESULTS_PER_ENTITY]
    text = "\n".join(str(item)[:120] for item in capped)
    structured = [
        {
            "display": str(item)[:120],
            "data": item if isinstance(item, dict) else {},
        }
        for item in capped
    ]
    return text, structured


def _try_json_decode(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return _NOT_JSON


def _wrap_as_text_result(value: Any) -> tuple[str, list[dict[str, Any]]]:
    text = str(value)[:300] if value else ""
    return text, [{"display": text, "data": {}}] if text else []


def _format_dict_result(
    data: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    results = data.get("results", [])
    if results:
        return _format_generic_items(results)

    text = str(data)[:300]
    return text, [{"display": text, "data": data}] if text else []


def _parse_enrichment_result(raw: Any) -> tuple[str, list[dict[str, Any]]]:
    data = raw
    if isinstance(raw, str):
        data = _try_json_decode(raw)
        if data is _NOT_JSON:
            return _wrap_as_text_result(raw)

    if isinstance(data, dict):
        # A failed or empty lookup is no evidence, not an item to cite.
        if (error := reported_failure(data)) is not None:
            logger.debug("context enrichment lookup failed: %s", error)
            return "", []
        if data.get("records") == []:
            return "", []
        return _format_dict_result(data)

    if isinstance(data, list):
        return _format_generic_items(data)

    return _wrap_as_text_result(data)


def _build_enrichment_canonical_params(entity: str) -> dict[str, Any]:
    """Tool mappings must accept this path's canonical vocabulary; mismatched
    mappings cause server refusals, not empty evidence."""
    return {
        "entity_name": entity,
        "limit": _CONTEXT_ENRICHMENT_RESULTS_PER_ENTITY,
    }


async def _query_enrichment_entity(
    tool_config: Any,
    entity: str,
    mcp_client: MCPToolClient,
) -> tuple[str, list[dict[str, Any]]]:
    """Citation building needs the originating YAML tool ID, which the
    aggregate stamps after entity-level retrieval."""
    tool_name = tool_config.mcp_tool_name
    params = tool_config.map_parameters(_build_enrichment_canonical_params(entity))
    raw = await _call_enrichment_tool_for_entity(tool_name, params, mcp_client)
    if raw is None:
        return "", []
    text, items = _parse_enrichment_result(raw)
    for item in items:
        item.setdefault("entity", entity)
    return text, items


async def _call_enrichment_tool_for_entities(
    tool_config: Any,
    entities: list[str],
    mcp_client: MCPToolClient,
) -> tuple[str, list[dict[str, Any]]]:

    per_entity = await asyncio.gather(
        *[_query_enrichment_entity(tool_config, entity, mcp_client) for entity in entities]
    )

    text_lines: list[str] = []
    all_items: list[dict[str, Any]] = []
    for entity, (text, items) in zip(entities, per_entity, strict=True):
        if text:
            text_lines.append(f"[{entity}]\n{text}")
        all_items.extend(items)

    return "\n\n".join(text_lines), all_items


def _resolve_enrichment_tool_configs(
    workflow: "WorkflowConfig",
    tool_registry: "ToolRegistry",
    mcp_client: MCPToolClient,
) -> list["ToolConfig"]:
    tool_configs = []
    for tool_id in workflow.context_enrichment_tools:
        tc = tool_registry.get_tool(tool_id)
        if tc and tc.enabled and mcp_client.has_tool(tc.mcp_tool_name):
            tc._yaml_tool_id = tool_id
            tool_configs.append(tc)
        else:
            logger.debug("context enrichment: tool '%s' unavailable or disabled", tool_id)
    return tool_configs


def _aggregate_enrichment_results(
    tool_configs: list["ToolConfig"],
    tool_results: list[Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    sections: list[str] = []
    all_structured: list[dict[str, Any]] = []
    for tc, result in zip(tool_configs, tool_results, strict=True):
        if isinstance(result, BaseException):
            logger.debug("context enrichment: %s raised %s", tc.mcp_tool_name, result)
            continue
        text, items = result
        if text:
            sections.append(f"**{tc.display_name}**\n{text}")

        yaml_tool_id = getattr(tc, "_yaml_tool_id", tc.mcp_tool_name)
        for item in items:
            item["tool_id"] = yaml_tool_id
        all_structured.extend(items)
    return sections, all_structured


def _resolve_enrichment_context(
    state: WorkflowState,
    config: SearchConfig,
) -> "tuple[WorkflowConfig, ToolRegistry, list[str]] | None":
    workflow = config.workflow
    if not workflow or not workflow.context_enrichment_tools:
        return None

    tool_registry = config.tool_registry
    if not tool_registry:
        return None

    # Goal-derived entities let enrichment run independently of paper retrieval.

    entities = extract_entity_names(state["research_goal"], max_entities=3)
    if not entities:
        logger.debug("context enrichment: no entities extracted from research goal")
        return None

    return workflow, tool_registry, entities


async def _run_enrichment_tools(
    entities: list[str],
    tool_configs: list["ToolConfig"],
    mcp_client: MCPToolClient,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Optional enrichment failures must not discard successful sibling
    tools."""
    tool_tasks = [
        _call_enrichment_tool_for_entities(tc, entities, mcp_client) for tc in tool_configs
    ]
    tool_results = await asyncio.gather(*tool_tasks, return_exceptions=True)
    return _aggregate_enrichment_results(tool_configs, tool_results)


def _cap_enrichment_text(combined: str) -> str:
    """Bound enrichment so it cannot crowd paper analyses out of the
    synthesis prompt."""
    if len(combined) > _CONTEXT_ENRICHMENT_MAX_CHARS:
        return combined[:_CONTEXT_ENRICHMENT_MAX_CHARS] + "\n[...truncated]"
    return combined


async def _gather_enrichment_content(
    workflow: "WorkflowConfig",
    tool_registry: "ToolRegistry",
    entities: list[str],
    mcp_client: MCPToolClient,
) -> tuple[list[str], list[dict[str, Any]]] | None:
    tool_configs = _resolve_enrichment_tool_configs(workflow, tool_registry, mcp_client)
    if not tool_configs:
        return None
    return await _run_enrichment_tools(entities, tool_configs, mcp_client)


def _finalize_enrichment_output(
    sections: list[str],
    all_structured: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    combined = _cap_enrichment_text("\n\n".join(sections))
    logger.info(
        "Phase 2.6 complete: %s tool(s), %s structured items (%s chars)",
        len(sections),
        len(all_structured),
        len(combined),
    )
    return combined, all_structured


async def _phase2_6_fetch_context_enrichment(
    state: WorkflowState,
    config: SearchConfig,
    mcp_client: MCPToolClient,
) -> tuple[str, list[dict[str, Any]]]:
    empty: tuple[str, list[dict[str, Any]]] = ("", [])

    resolved = _resolve_enrichment_context(state, config)
    if resolved is None:
        return empty
    workflow, tool_registry, entities = resolved

    logger.info(
        "Phase 2.6: fetching context enrichment for entities %s via %s tool(s)",
        entities,
        len(workflow.context_enrichment_tools),
    )

    gathered = await _gather_enrichment_content(workflow, tool_registry, entities, mcp_client)
    if gathered is None:
        return empty
    sections, all_structured = gathered
    if not sections and not all_structured:
        return empty

    return _finalize_enrichment_output(sections, all_structured)


def _format_kg_section_with_keys(
    context_enrichment_sources: list[dict[str, Any]],
    paper_count: int,
) -> str:
    """Enrichment starts after analyzed papers so synthesis and generation
    expose the same [C*] citation handles."""
    if not context_enrichment_sources:
        return ""
    lines = []
    for i, item in enumerate(context_enrichment_sources):
        key = f"C{paper_count + i + 1}"
        display = item.get("display", "External source")
        lines.append(f"[{key}] {display}")
    return "\n\n---\n\n## Knowledge Graph Evidence\n\n" + "\n\n".join(lines)
