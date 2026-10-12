import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, TypeVar, cast


def reported_failure(payload: Any) -> str | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if isinstance(payload, dict) and payload.get("status") == "failed":
        return str(payload.get("error") or "search failed")
    return None


def describe_exception(exc: BaseException) -> str:
    """AnyIO MCP task groups wrap failures in ExceptionGroups; the first leaf
    exposes the actual network or validation cause."""
    current: BaseException = exc

    while getattr(current, "exceptions", None):
        current = current.exceptions[0]  # type: ignore[attr-defined]
    message = str(current).strip()
    return f"{type(current).__name__}: {message}" if message else type(current).__name__


if TYPE_CHECKING:
    from co_scientist.platform.retrieval.config import (
        SearchSourceConfig,
        ToolRegistry,
        WorkflowConfig,
    )

logger = logging.getLogger(__name__)

_ConfigT = TypeVar("_ConfigT")
_EntryT = TypeVar("_EntryT")


_SourceToolResolver = Callable[
    [Optional["SearchSourceConfig"], "WorkflowConfig", "ToolRegistry"],
    _EntryT | None,
]


def _lookup_source_config(
    pid: str,
    paper_source_map: dict[str, str],
    config: dict[str, _ConfigT],
) -> _ConfigT | None:
    """Route each paper by its originating source; single-source mode uses
    _default."""
    source_tool_id = paper_source_map.get(pid, "_default")
    return config.get(source_tool_id) or config.get("_default")


def _select_eligible_papers(
    all_paper_metadata: dict[str, dict[str, Any]],
    paper_source_map: dict[str, str],
    config: dict[str, _ConfigT],
    resolve: Callable[
        [str, dict[str, Any], dict[str, str], dict[str, _ConfigT]],
        _EntryT | None,
    ],
) -> list[_EntryT]:
    entries = (
        resolve(pid, meta, paper_source_map, config) for pid, meta in all_paper_metadata.items()
    )
    return [entry for entry in entries if entry is not None]


@dataclass(frozen=True)
class _SourceToolFields:
    tool_attr: str
    url_field_attr: str


_PDF_FIELDS = _SourceToolFields(
    tool_attr="pdf_discovery_tool",
    url_field_attr="pdf_discovery_url_field",
)
_CONTENT_FIELDS = _SourceToolFields(
    tool_attr="content_tool",
    url_field_attr="content_url_field",
)


def _source_or_workflow(
    source: Optional["SearchSourceConfig"],
    workflow: "WorkflowConfig",
    attr: str,
) -> Any:
    override = getattr(source, attr, None) if source is not None else None
    return override or getattr(workflow, attr, None)


def _resolve_source_tool(
    source: Optional["SearchSourceConfig"],
    workflow: "WorkflowConfig",
    tool_registry: "ToolRegistry",
    fields: _SourceToolFields,
) -> tuple[str, str] | None:
    tool_id = _source_or_workflow(source, workflow, fields.tool_attr)
    if not tool_id:
        return None
    tool_cfg = tool_registry.get_tool(tool_id)
    if not tool_cfg:
        return None
    url_field = _source_or_workflow(source, workflow, fields.url_field_attr)
    return tool_cfg.mcp_tool_name, url_field


def _build_source_config(
    workflow: Optional["WorkflowConfig"],
    tool_registry: Optional["ToolRegistry"],
    is_multi_source: bool,
    resolve: _SourceToolResolver[_EntryT],
) -> dict[str, _EntryT]:
    """Sources differ in landing-page layout and content-tool requirements;
    resolve retrieval configuration per originating source."""
    if not workflow or not tool_registry:
        return {}

    if not is_multi_source:
        default = resolve(None, workflow, tool_registry)
        return {"_default": default} if default else {}

    config: dict[str, _EntryT] = {}
    for source in workflow.get_enabled_search_sources():
        resolved = resolve(source, workflow, tool_registry)
        if resolved:
            config[source.tool] = resolved
    return config


def _resolve_pdf_discovery_tool(
    source: Optional["SearchSourceConfig"],
    workflow: "WorkflowConfig",
    tool_registry: "ToolRegistry",
) -> tuple[str, str] | None:
    """PDF discovery takes only the landing URL; extra parameters belong to
    content retrieval."""
    return _resolve_source_tool(source, workflow, tool_registry, _PDF_FIELDS)


def build_pdf_discovery_config(
    workflow: Optional["WorkflowConfig"],
    tool_registry: Optional["ToolRegistry"],
    is_multi_source: bool,
) -> dict[str, tuple[str, str]]:
    return _build_source_config(
        workflow, tool_registry, is_multi_source, _resolve_pdf_discovery_tool
    )


def _resolve_pdf_discovery_entry(
    pid: str,
    meta: dict[str, Any],
    paper_source_map: dict[str, str],
    pdf_discovery_config: dict[str, tuple[str, str]],
) -> tuple[str, dict[str, Any], str, str] | None:
    if not isinstance(meta, dict) or meta.get("pdf_url"):
        return None

    config = _lookup_source_config(pid, paper_source_map, pdf_discovery_config)
    if not config:
        return None

    tool_name, url_field = config
    landing_url = meta.get(url_field)
    if not landing_url:
        return None
    return pid, meta, tool_name, url_field


def get_papers_needing_pdf_discovery(
    all_paper_metadata: dict[str, dict[str, Any]],
    paper_source_map: dict[str, str],
    pdf_discovery_config: dict[str, tuple[str, str]],
) -> list[tuple[str, dict[str, Any], str, str]]:
    return _select_eligible_papers(
        all_paper_metadata,
        paper_source_map,
        pdf_discovery_config,
        _resolve_pdf_discovery_entry,
    )


def _first_link_url(link: Any) -> str | None:
    return link if isinstance(link, str) else link.get("url")


def _pdf_url_from_list(result_data: list[Any]) -> str | None:
    if not result_data:
        return None
    return cast(str | None, result_data[0])


def _pdf_url_from_dict(result_data: dict[str, Any]) -> str | None:
    links = result_data.get("pdf_links") or result_data.get("links") or []
    if not links:
        return None
    return _first_link_url(links[0])


def _pdf_url_from_parsed(result_data: Any) -> str | None:
    if isinstance(result_data, list):
        return _pdf_url_from_list(result_data)
    if isinstance(result_data, dict):
        return _pdf_url_from_dict(result_data)
    return None


def _parse_pdf_url_from_string(result: str) -> str | None:
    try:
        result_data = json.loads(result)
    except json.JSONDecodeError:
        return result if result.startswith("http") else None
    return _pdf_url_from_parsed(result_data)


def parse_pdf_discovery_result(result: Any) -> str | None:
    """MCP tools serialize discovery as raw JSON, parsed collections or bare
    URLs."""
    if isinstance(result, str):
        return _parse_pdf_url_from_string(result)
    if isinstance(result, list) and result:
        return _first_link_url(result[0])
    return None


@dataclass
class ContentToolConfig:
    mcp_tool_name: str
    url_field: str
    content_params: dict[str, Any]


def _resolve_content_tool(
    source: Optional["SearchSourceConfig"],
    workflow: "WorkflowConfig",
    tool_registry: "ToolRegistry",
) -> ContentToolConfig | None:
    resolved = _resolve_source_tool(source, workflow, tool_registry, _CONTENT_FIELDS)
    if not resolved:
        return None
    mcp_tool_name, url_field = resolved
    src_params = source.content_params if source else {}
    return ContentToolConfig(
        mcp_tool_name=mcp_tool_name,
        url_field=url_field,
        content_params={**workflow.content_params, **src_params},
    )


def build_content_config(
    workflow: Optional["WorkflowConfig"],
    tool_registry: Optional["ToolRegistry"],
    is_multi_source: bool,
) -> dict[str, ContentToolConfig]:
    return _build_source_config(workflow, tool_registry, is_multi_source, _resolve_content_tool)


def _resolve_content_entry(
    pid: str,
    meta: dict[str, Any],
    paper_source_map: dict[str, str],
    content_config: dict[str, ContentToolConfig],
) -> tuple[str, dict[str, Any], ContentToolConfig] | None:
    if not isinstance(meta, dict) or meta.get("fulltext"):
        return None

    cfg = _lookup_source_config(pid, paper_source_map, content_config)
    if not cfg:
        return None

    content_url = meta.get(cfg.url_field)
    if not content_url:
        return None
    return pid, meta, cfg


def get_papers_needing_content(
    all_paper_metadata: dict[str, dict[str, Any]],
    paper_source_map: dict[str, str],
    content_config: dict[str, ContentToolConfig],
) -> list[tuple[str, dict[str, Any], ContentToolConfig]]:
    return _select_eligible_papers(
        all_paper_metadata,
        paper_source_map,
        content_config,
        _resolve_content_entry,
    )


def _parse_content_from_string(result: str) -> str | None:
    try:
        result_data = json.loads(result)
    except json.JSONDecodeError:
        return result
    if isinstance(result_data, dict):
        return _parse_content_from_dict(result_data)
    return result


def _parse_content_from_dict(result: dict[str, Any]) -> str | None:
    if result.get("status") == "failed":
        return None
    if result.get("status") == "ok":
        return (
            "\n\n".join(
                str(record.get("content") or record.get("text") or record.get("fulltext") or "")
                for record in result["records"]
            )
            or None
        )
    field = cast(str | None, result.get("content") or result.get("text"))
    return field or str(result)


def parse_content_result(result: Any) -> str | None:

    if isinstance(result, str):
        return _parse_content_from_string(result)
    if isinstance(result, dict):
        return _parse_content_from_dict(result)
    return str(result) if result else None
