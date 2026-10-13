import datetime
import re
from dataclasses import dataclass, field, fields
from typing import Any


def _declared_field_kwargs(
    cls: type[Any],
    data: dict[str, Any] | None,
    *,
    exclude: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Presence beats defaults, including explicit null; unknown YAML keys
    remain tolerated.
    """
    names = {f.name for f in fields(cls)} - set(exclude)
    return {key: value for key, value in (data or {}).items() if key in names}


def _tolerant_field_kwargs(
    cls: type[Any],
    data: dict[str, Any],
    required: str,
) -> dict[str, Any]:
    """YAML tolerates omitted fields that are required by dataclass
    constructors.
    """
    return {
        required: data.get(required, ""),
        **_declared_field_kwargs(cls, data, exclude=(required,)),
    }


_PLACEHOLDER_PATTERN = re.compile(r"\{(\w+)\}")


def _substitute_placeholders(
    value: str, context: dict[str, Any], *, preserve_type: bool = True
) -> Any:
    exact = _PLACEHOLDER_PATTERN.fullmatch(value)
    if preserve_type and exact and exact[1] in context:
        return context[exact[1]]
    # Preserve occurrence order: earlier values can introduce later-resolvable
    # placeholders.
    for name in _PLACEHOLDER_PATTERN.findall(value):
        if name in context:
            value = value.replace(f"{{{name}}}", str(context[name]))
    return value


def resolve_content_params(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not params:
        return {}
    resolved: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str):
            resolved[key] = _substitute_placeholders(value, context)
        elif isinstance(value, list):
            resolved[key] = [
                _substitute_placeholders(item, context, preserve_type=False)
                if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            resolved[key] = value
    return resolved


@dataclass
class ServerConfig:
    url: str
    transport: str = "streamable_http"
    enabled: bool = True


@dataclass
class ResponseFormat:
    type: str = "json"
    results_path: str = "."
    is_dict: bool = False
    field_mapping: dict[str, str] = field(default_factory=dict)


@dataclass
class ParameterConfig:
    type: str = "string"
    default: Any | None = None
    required: bool = False
    description: str = ""
    minimum: int | None = None
    maximum: int | None = None


def _recency_years_to_starting_year(value: int) -> int | None:
    current_year = datetime.datetime.now().year
    return current_year - value if value > 0 else None


@dataclass
class ToolConfig:
    server: str
    mcp_tool_name: str
    display_name: str = ""
    description: str = ""
    category: str = "utility"
    source_type: str = "academic"
    enabled: bool = True
    # Missing effects must serialize: latency is safer than parallel execution
    # with side effects.
    effects: list[str] = field(default_factory=lambda: ["write"])
    response_format: ResponseFormat = field(default_factory=ResponseFormat)
    prompt_snippet: str = ""
    parameters: dict[str, ParameterConfig] = field(default_factory=dict)
    parameter_mapping: dict[str, str | None] = field(default_factory=dict)
    applies_to: str = "all"
    # Keep the YAML tool identity for downstream citation construction.
    _yaml_tool_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], tool_id: str = "") -> "ToolConfig":
        params_data = data.get("parameters", {})
        parameters = {}
        for param_name, param_data in params_data.items():
            if isinstance(param_data, dict):
                parameters[param_name] = ParameterConfig(
                    **_declared_field_kwargs(ParameterConfig, param_data)
                )
            else:
                parameters[param_name] = ParameterConfig(default=param_data)

        # YAML permits server defaults and tool-ID names that differ from
        # dataclass defaults.
        kwargs = _declared_field_kwargs(
            cls,
            data,
            exclude=(
                "server",
                "mcp_tool_name",
                "display_name",
                "response_format",
                "parameters",
                "_yaml_tool_id",
            ),
        )
        return cls(
            server=data.get("server", "default"),
            mcp_tool_name=data.get("mcp_tool_name", tool_id),
            display_name=data.get("display_name", tool_id),
            response_format=ResponseFormat(
                **_declared_field_kwargs(ResponseFormat, data.get("response_format", {}))
            ),
            parameters=parameters,
            **kwargs,
        )

    # Mappings use caller-canonical vocabulary; unmapped parameters pass through
    # to the server.
    def map_parameters(self, canonical_params: dict[str, Any]) -> dict[str, Any]:
        if not self.parameter_mapping:
            return canonical_params

        mapped = {}
        for canonical_name, value in canonical_params.items():
            tool_param_name, mapped_value = self._map_single_parameter(canonical_name, value)
            if tool_param_name is not None:
                mapped[tool_param_name] = mapped_value

        return mapped

    def _map_single_parameter(self, canonical_name: str, value: Any) -> tuple[str | None, Any]:
        if canonical_name not in self.parameter_mapping:
            return canonical_name, value

        tool_param_name = self.parameter_mapping[canonical_name]
        if tool_param_name is None:
            return None, None

        if canonical_name == "recency_years" and tool_param_name == "starting_year":
            return tool_param_name, _recency_years_to_starting_year(value)

        return tool_param_name, value


@dataclass
class SearchSourceConfig:
    """Local evidence lacks citation/year scores; reserved slots prevent
    indexed papers crowding it out.
    """

    tool: str
    papers_per_query: int = 3
    enabled: bool = True
    reserved_slots: int = 0
    content_tool: str | None = None
    content_url_field: str | None = None
    content_params: dict[str, Any] = field(default_factory=dict)
    pdf_discovery_tool: str | None = None
    pdf_discovery_url_field: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str) -> "SearchSourceConfig":
        if isinstance(data, str):
            return cls(tool=data)
        return cls(**_tolerant_field_kwargs(cls, data, "tool"))


def _search_source_tool_ids(sources: list["SearchSourceConfig"]) -> list[str]:
    tool_ids = []
    for source in sources:
        tool_ids.append(source.tool)
        if source.content_tool:
            tool_ids.append(source.content_tool)
    return tool_ids


@dataclass
class WorkflowConfig:
    # Only one source mode is active; nonempty search_sources selects multi-
    # source behavior.
    primary_search: str | None = None
    fallback_search: str | None = None
    availability_check: str | None = None

    search_sources: list[SearchSourceConfig] = field(default_factory=list)
    deduplicate_across_sources: bool = True

    search_tools: list[str] = field(default_factory=list)
    read_tools: list[str] = field(default_factory=list)
    utility_tools: list[str] = field(default_factory=list)

    # External context is fetched once per phase rather than per hypothesis.
    context_enrichment_tools: list[str] = field(default_factory=list)

    query_generation_tool: str | None = None
    query_format: str = "boolean"

    content_tool: str | None = None
    content_url_field: str = "pdf_url"
    content_params: dict[str, Any] = field(default_factory=dict)
    pdf_discovery_tool: str | None = None
    pdf_discovery_url_field: str = "url"

    # Landing-page sources need PDF discovery before content retrieval.

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowConfig":
        search_sources = [
            SearchSourceConfig.from_dict(source_data)
            for source_data in (data or {}).get("search_sources", [])
        ]

        return cls(
            search_sources=search_sources,
            **_declared_field_kwargs(cls, data, exclude=("search_sources",)),
        )

    def get_enabled_search_sources(self) -> list[SearchSourceConfig]:
        return [s for s in self.search_sources if s.enabled]

    def is_multi_source(self) -> bool:
        return len(self.search_sources) > 0

    def get_all_tools(self) -> list[str]:
        single_tool_fields = (
            self.primary_search,
            self.fallback_search,
            self.availability_check,
            self.query_generation_tool,
            self.content_tool,
        )
        tools = [tool_id for tool_id in single_tool_fields if tool_id]
        tools.extend(_search_source_tool_ids(self.search_sources))
        tools.extend(self.search_tools)
        tools.extend(self.read_tools)
        tools.extend(self.utility_tools)
        tools.extend(self.context_enrichment_tools)
        return tools


@dataclass
class EnrichmentConfig:
    tool: str
    input_field: str = "text"
    output_key: str = ""
    enabled: bool = True
    max_results: int = 10
    results_path: str = ""
    workflow: str = "generation"


@dataclass
class PromptsConfig:
    domain_context: str = ""
    generation_guidance: str = ""
    review_guidance: str = ""
    evolution_guidance: str = ""
    reflection_guidance: str = ""


__all__ = [
    "EnrichmentConfig",
    "ParameterConfig",
    "PromptsConfig",
    "ResponseFormat",
    "SearchSourceConfig",
    "ServerConfig",
    "ToolConfig",
    "ToolsConfig",
    "WorkflowConfig",
    "resolve_content_params",
]


def _parse_servers(data: dict[str, Any]) -> dict[str, ServerConfig]:
    servers = {}
    for server_id, server_data in data.get("servers", {}).items():
        servers[server_id] = ServerConfig(
            **_tolerant_field_kwargs(ServerConfig, server_data, "url")
        )
    return servers


def _parse_workflows(data: dict[str, Any]) -> dict[str, WorkflowConfig]:
    workflows = {}
    for workflow_id, workflow_data in data.get("workflows", {}).items():
        workflows[workflow_id] = WorkflowConfig.from_dict(workflow_data)
    return workflows


def _parse_tools_by_category(
    data: dict[str, Any],
) -> dict[str, dict[str, ToolConfig]]:
    tools: dict[str, dict[str, ToolConfig]] = {}
    tools_data = data.get("tools", {})
    for category, category_tools in tools_data.items():
        tools[category] = {}
        for tool_id, tool_data in category_tools.items():
            tools[category][tool_id] = ToolConfig.from_dict(tool_data, tool_id)
    return tools


@dataclass
class ToolsConfig:
    # Tool IDs must be unique across categories for unambiguous lookup.
    version: str = "1.0"
    servers: dict[str, ServerConfig] = field(default_factory=dict)
    tools: dict[str, dict[str, ToolConfig]] = field(default_factory=dict)
    workflows: dict[str, WorkflowConfig] = field(default_factory=dict)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    enrichments: list[EnrichmentConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolsConfig":
        servers = _parse_servers(data)
        tools = _parse_tools_by_category(data)
        workflows = _parse_workflows(data)
        prompts = PromptsConfig(**_declared_field_kwargs(PromptsConfig, data.get("prompts", {})))
        enrichments = [
            EnrichmentConfig(**_tolerant_field_kwargs(EnrichmentConfig, e, "tool"))
            for e in data.get("enrichments", [])
        ]

        kwargs = _declared_field_kwargs(
            cls,
            data,
            exclude=(
                "servers",
                "tools",
                "workflows",
                "settings",
                "prompts",
                "enrichments",
            ),
        )
        return cls(
            servers=servers,
            tools=tools,
            workflows=workflows,
            prompts=prompts,
            enrichments=enrichments,
            **kwargs,
        )

    def get_tool(self, tool_id: str) -> ToolConfig | None:
        for category_tools in self.tools.values():
            if tool_id in category_tools:
                return category_tools[tool_id]
        return None

    def get_all_tools(self) -> dict[str, ToolConfig]:
        all_tools = {}
        for category_tools in self.tools.values():
            all_tools.update(category_tools)
        return all_tools

    def get_enabled_tools(self) -> dict[str, ToolConfig]:
        return {tool_id: tool for tool_id, tool in self.get_all_tools().items() if tool.enabled}
