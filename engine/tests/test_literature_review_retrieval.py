from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import co_scientist.platform.retrieval.evidence as evidence
from co_scientist.core.constants import SOURCE_FANOUT
from co_scientist.orchestration.generator.initial_state import (
    RunCapabilities,
    RunIdentity,
    _build_initial_state,
)
from co_scientist.platform.retrieval.config import SearchSourceConfig, ToolRegistry, WorkflowConfig
from co_scientist.platform.retrieval.degradation import (
    CAPABILITIES_LOST_WITHOUT_MCP,
    FLOOR_NONE,
    FLOOR_RUN_ATTACHMENTS,
    MCP_UNREACHABLE,
)
from co_scientist.platform.retrieval.evidence import search
from co_scientist.platform.retrieval.mcp_client import MCPToolClient
from co_scientist.science import evidence_context as probes
from tests._llm_fake import install_fake_llm
from tests._mcp import make_tool_lookup_registry
from tests._research_fakes import (
    make_search_config,
    make_search_run_ctx,
    make_tool_config,
    make_two_source_workflow,
)
from tests._state import make_state


async def test_probe_search_preserves_sources_and_excludes_retractions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = {
        "W123": {
            "title": "Current evidence",
            "abstract": "A measured result.",
            "_source_name": "openalex",
        },
        "retracted": {
            "title": "Retracted evidence",
            "abstract": "A withdrawn result.",
            "is_retracted": True,
        },
    }

    async def collect(*args: Any) -> tuple[Any, Any]:
        assert args[2].semantic_relevance_enabled is False
        assert args[2].papers_to_read_count == 6
        args[4].append("One source unavailable")
        return records, {}

    monkeypatch.setattr(
        "co_scientist.platform.retrieval.evidence.search.collect_papers",
        collect,
    )
    monkeypatch.setattr(
        "co_scientist.platform.retrieval.mcp_client.get_mcp_client",
        AsyncMock(return_value=object()),
    )
    articles, errors = await probes._retrieve_probe_evidence(
        make_state(mcp_available=True), ["measured result"]
    )
    assert [(a.source, a.source_id) for a in articles] == [("openalex", "W123")]
    assert errors == ["One source unavailable"]


def _state(*, mcp_available: bool, opts: dict[str, Any] | None = None) -> Any:
    return _build_initial_state(
        config_fields={},
        identity=RunIdentity(research_goal="reverse fibrosis", start_time=0.0, run_id="run-1"),
        capabilities=RunCapabilities(mcp_available=mcp_available),
        opts=opts or {},
        user_inputs={},
    )


@pytest.mark.parametrize(
    ("mcp_available", "opts", "floor"),
    [
        (False, None, FLOOR_NONE),
        (
            False,
            {"context_enrichment_sources": [{"title": "a memo"}]},
            FLOOR_RUN_ATTACHMENTS,
        ),
    ],
    ids=["no-documents-of-its-own", "run-attachments"],
)
def test_a_run_that_cannot_retrieve_names_what_it_lost(
    mcp_available: bool, opts: dict[str, Any] | None, floor: str
) -> None:
    """Ideas and reviews can look healthy without retrieval; the loss must
    reach the report."""
    degradation = _state(mcp_available=mcp_available, opts=opts)["retrieval_degradation"]

    assert _state(mcp_available=True)["retrieval_degradation"] is None
    assert degradation is not None
    assert degradation["reason"] == MCP_UNREACHABLE
    assert degradation["lost"] == list(CAPABILITIES_LOST_WITHOUT_MCP)
    assert "literature_review" in degradation["lost"]
    assert "deep_research" in degradation["lost"]
    assert degradation["floor"] == floor
    assert json.loads(json.dumps(degradation)) == degradation


@pytest.fixture(autouse=True)
def _hermetic_node_model(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_llm(monkeypatch)


class _SequencedMCPClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        self.calls.append((tool_name, kwargs))
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _collect_multi_source(
    registry: Any,
    client: Any,
    errors: list[str],
    *,
    papers_per_query: int,
    semantic: bool = True,
) -> tuple[dict[str, Any], dict[str, str]]:
    config = make_search_config(
        tool_registry=cast(ToolRegistry, registry),
        workflow=make_two_source_workflow(papers_per_query),
        is_multi_source=True,
        search_tool_name="unused",
        source_name="mixed",
        papers_to_read_count=10,
    )
    config.semantic_relevance_enabled = semantic
    return await search._phase2_collect_papers_multi_source(
        ["q1"], config, make_search_run_ctx(client, errors, run_id="run-1")
    )


async def test_a_failing_source_keeps_its_healthy_sibling_and_diagnostics() -> None:
    registry = make_tool_lookup_registry(
        {
            "src_a": make_tool_config(mcp_tool_name="search_europepmc"),
            "src_b": make_tool_config(mcp_tool_name="search_pubmed"),
        }
    )

    class Client:
        async def call_tool(self, name: str, **_: Any) -> Any:
            if name == "search_europepmc":
                return {"status": "failed", "records": [], "error": "Europe PMC HTTP 503"}
            return {"P1": {"title": "Healthy source paper"}}

    errors: list[str] = []

    papers, sources = await _collect_multi_source(
        registry, Client(), errors, papers_per_query=1, semantic=False
    )

    assert set(papers) == {"P1"}
    assert sources == {"P1": "src_b"}
    assert len(errors) == 1
    assert "search_europepmc" in errors[0] and "Europe PMC" in errors[0]
    assert "HTTP 503" in errors[0]


async def test_identical_queries_are_sent_once_and_sources_fan_out_in_bounded_number() -> None:
    sources = [f"src_{index}" for index in range(SOURCE_FANOUT + 2)]
    registry = make_tool_lookup_registry(
        {source: make_tool_config(mcp_tool_name=f"search_{source}") for source in sources}
    )
    sent: list[tuple[str, str]] = []
    in_flight = 0
    peak = 0

    class Client:
        async def call_tool(self, name: str, **kwargs: Any) -> Any:
            nonlocal in_flight, peak
            sent.append((name, kwargs["query"]))
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return {f"{name}-{kwargs['query']}": {"title": "A paper"}}

    config = make_search_config(
        tool_registry=cast(ToolRegistry, registry),
        workflow=WorkflowConfig(
            search_sources=[
                SearchSourceConfig(tool=source, papers_per_query=1) for source in sources
            ],
            deduplicate_across_sources=True,
        ),
        is_multi_source=True,
        search_tool_name="unused",
        source_name="mixed",
        papers_to_read_count=20,
    )
    config.semantic_relevance_enabled = False
    state = make_state(research_goal="fibrosis", run_id="run-1")

    await search.collect_papers(
        ["fibrosis  TGF-beta", "fibrosis TGF-beta", "collagen"],
        state,
        config,
        cast(MCPToolClient, Client()),
        [],
    )

    assert sorted(sent) == sorted(
        (f"search_{source}", query)
        for source in sources
        for query in ("fibrosis  TGF-beta", "collagen")
    )
    assert peak <= SOURCE_FANOUT * 2


_HTTP_503 = {"kind": "http_status", "status_code": 503, "detail": "HTTP 503"}


@pytest.mark.parametrize(
    "failed",
    [
        {"status": "failed", "records": [], "error": "Europe PMC HTTP 503"},
        # Keyed-by-id tools carry only the error.
        {"status": "failed", "records": [], "error": "HTTP 503"},
        json.dumps({"status": "failed", "records": [], "error": "HTTP 503"}),
    ],
)
async def test_a_failed_lookup_is_recorded_once_not_broadened_as_no_match(failed: Any) -> None:
    registry = make_tool_lookup_registry(
        {"src_a": make_tool_config(mcp_tool_name="search_europepmc")}
    )
    calls: list[str] = []

    class Client:
        async def call_tool(self, name: str, **_: Any) -> Any:
            calls.append(name)
            return failed

    errors: list[str] = []

    papers, _ = await _collect_multi_source(
        registry, Client(), errors, papers_per_query=1, semantic=False
    )

    assert papers == {}
    assert calls == ["search_europepmc"]
    assert len(errors) == 1 and "HTTP 503" in errors[0]


_AGENT_MODULES = (
    *(
        f"co_scientist.science.{agent}"
        for agent in (
            "generation",
            "reflection",
            "ranking",
            "evolution",
            "proximity",
            "meta_review",
            "supervisor",
            "safety_screen",
            "node_degradation",
        )
    ),
    "co_scientist.orchestration.registry",
)


def test_shared_evidence_modules_do_not_import_agents() -> None:
    for path in Path(evidence.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        modules += [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not any(module.startswith(_AGENT_MODULES) for module in modules), path
