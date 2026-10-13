from __future__ import annotations

from pathlib import Path

import pytest

from co_scientist.core.exceptions import ConfigError
from co_scientist.platform.retrieval.config import ToolRegistry
from tests._research_fakes import REPLACE_CONFIG as _REPLACE_CONFIG
from tests._research_fakes import write_config as _write_config


@pytest.fixture()
def _config_registry_registry(tmp_path: Path) -> ToolRegistry:
    path = _write_config(tmp_path, _REPLACE_CONFIG)
    return ToolRegistry(config_path=path)


def test_registry_lookups_over_a_loaded_config(
    _config_registry_registry: ToolRegistry,
) -> None:
    registry = _config_registry_registry
    assert registry.get_server_configs_for_langchain() == {
        "myserver": {
            "transport": "streamable_http",
            "url": "http://example.test/mcp",
        }
    }
    assert set(registry.get_enabled_servers()) == {"myserver"}
    alpha = registry.get_tool_by_mcp_name("search_alpha")
    assert alpha is not None and alpha.display_name == "Alpha Search"
    assert registry.get_tool("pubmed_search") is None
    assert registry.get_tools_for_workflow("does_not_exist") == []
    assert registry.get_prompts_config().domain_context == ("test domain context")
    assert [e.output_key for e in registry.get_enrichment_configs("all")] == [
        "gamma_out",
        "alpha_out",
    ]
    registry._config = None
    with pytest.raises(ConfigError):
        _ = registry.config


def test_default_registry_loads_bundled_tools() -> None:
    assert ToolRegistry().get_tool("pubmed_search") is not None


def test_disabled_tools_argument_removes_tool_from_workflows(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(
        config_path=_write_config(tmp_path, _REPLACE_CONFIG),
        disabled_tools=["alpha_search"],
    )
    assert registry.get_tools_for_workflow("literature_review") == [
        "gamma_util",
    ]
