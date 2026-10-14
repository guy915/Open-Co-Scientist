"""Test doubles that let an offline run take the same branches as a production run.

The offline backend answers every enum with its first value, so a ranking judge
always picks the first-presented side and a debate never reaches consensus; a
real judge prefers one idea whichever side it sits on. Without an MCP server an
offline run also skips literature review, observation reviews and every evidence
query, which production runs always pay for.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, ClassVar

from langchain_core.tools import StructuredTool

_SIDES = re.compile(r"Hypothesis 1:\s*\n(?P<a>.*?)\n\s*\nHypothesis 2:\s*\n(?P<b>.*?)\n\s*\n", re.S)
_RANKING_SCHEMAS = frozenset({"ranking_judgment"})


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


_DEBATE_TURN = "collaborative discourse concerning the generation"


_CITATION_KEYS = frozenset({"supporting", "contradicting"})


def _cite_as_lists(value: Any) -> Any:
    """The offline filler leaves a `oneOf` field as `{}`; a real model lists
    its citations, so claim checks must not fail validation and retry."""
    if isinstance(value, dict):
        return {
            key: [] if key in _CITATION_KEYS and item == {} else _cite_as_lists(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_cite_as_lists(item) for item in value]
    return value


_PAPER_HEADING = re.compile(r"^### Paper (\d+)$", re.M)


def _analysis_per_paper(prompt: str, content: str) -> str:
    """The offline filler returns a fixed-length list; a real model analyzes
    every numbered paper."""
    answer = json.loads(content)
    template = (answer.get("analyses") or [{}])[0]
    answer["analyses"] = [
        {**template, "paper_index": int(number), "key_findings": f"Finding of paper {number}."}
        for number in _PAPER_HEADING.findall(prompt)
    ]
    return json.dumps(answer)


def realistic_answer(prompt: str, content: str, schema_name: str) -> str:
    """A real debate panel converges after its first exchange, and a real judge
    prefers the side whose text hashes lower, whichever position it holds."""
    if not schema_name and _DEBATE_TURN in prompt:
        return f"{content}\nHYPOTHESIS: the panel agrees on the proposal above."
    if schema_name == "response" and any(f'"{key}": {{}}' in content for key in _CITATION_KEYS):
        return json.dumps(_cite_as_lists(json.loads(content)))
    if schema_name == "paper_analysis_batch":
        return _analysis_per_paper(prompt, content)
    if schema_name not in _RANKING_SCHEMAS:
        return content
    sides = _SIDES.search(prompt)
    if sides is None:
        return content
    first = _digest(sides["a"]) <= _digest(sides["b"])
    answer = json.loads(content)
    answer["winner"] = "a" if first else "b"
    answer["decision_summary"] = f"Consistent offline verdict.\nbetter idea: {1 if first else 2}"
    return json.dumps(answer)


def _paper(tool: str, query: str, index: int) -> dict[str, Any]:
    key = hashlib.sha256(f"{tool}:{query}:{index}".encode()).hexdigest()[:10]
    return {
        "title": f"Evidence on {query[:80]} ({index})",
        "abstract": (
            f"This study reports measurements bearing on {query[:160]}. "
            "Perturbing the pathway shifted the readout in a dose-dependent way."
        ),
        "authors": ["A. Author", "B. Author"],
        "year": 2023,
        "date_revised": "2023/05/01",
        "url": f"https://example.org/{key}",
        "doi": f"10.0000/{key}",
        "publication": "Journal of Offline Results",
        "fulltext": "",
        "source_id": key,
    }


_SEARCH_TOOLS = frozenset(
    {
        "search_pubmed",
        "pubmed_search_with_fulltext",
        "search_openalex",
        "search_web",
        "search_europepmc",
        "search_preprints",
        "search_arxiv",
        "search_biorxiv",
    }
)


def _ok(records: list[dict[str, Any]]) -> str:
    # The MCP result contract (`engine/mcp_server/tools/_results.py`).
    return json.dumps({"status": "ok", "records": records})


def _tool(name: str) -> StructuredTool:
    async def _impl(**kwargs: Any) -> str:
        if name.startswith("check_"):
            return "true"
        query = str(kwargs.get("query") or kwargs.get("entity_name") or "topic")
        count = int(kwargs.get("max_papers") or kwargs.get("max_results") or 3)
        if name in _SEARCH_TOOLS:
            return _ok([_paper(name, query, index) for index in range(min(count, 4))])
        if name == "read_url":
            url = str(kwargs.get("url") or "")
            return _ok([{"url": url, "content": f"Page text about {query[:120]}."}])
        return _ok([])

    return StructuredTool.from_function(
        coroutine=_impl, name=name, description=f"offline {name}", infer_schema=False
    )


def mcp_tool_names() -> list[str]:
    from co_scientist.platform.retrieval.config import get_tool_registry

    tools = get_tool_registry().config.get_all_tools().values()
    return sorted({tool.mcp_tool_name for tool in tools if tool.mcp_tool_name})


class FakeMCPClient:
    names: ClassVar[list[str]] = []

    def __init__(self, connections: Any, **_options: Any) -> None:
        self.connections = connections

    async def get_tools(self) -> list[StructuredTool]:
        return [_tool(name) for name in type(self).names]
