from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from co_scientist.core.exceptions import ResponseParseError
from co_scientist.platform.retrieval import config as config_mod
from co_scientist.platform.retrieval.evidence import search_query
from co_scientist.science.generation import literature_tools
from co_scientist.science.generation.generate import generate_node
from co_scientist.science.generation.literature_tools import draft, validate
from tests._mcp import FakeToolResultsClient, make_tool_results_client
from tests._state import make_article, make_generation_response, make_state


@pytest.fixture
def tools_node(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    def no_registry() -> Any:
        raise RuntimeError("no global registry")

    monkeypatch.setattr(config_mod, "get_tool_registry", no_registry)

    async def run(
        *,
        draft_response: str = '{"drafts": [{"text": "draft"}]}',
        synthesis_response: str | None = None,
        client: Any = None,
        state_overrides: dict[str, Any] | None = None,
        draft_capture: list[dict[str, Any]] | None = None,
        synthesis_call: Any = None,
        novelty_call: Any = None,
    ) -> dict[str, Any]:
        if client is None:
            client = make_tool_results_client()

        async def get_client(**_: Any) -> Any:
            return client

        async def draft_call(**kwargs: Any) -> tuple[str, list[Any]]:
            if draft_capture is not None:
                draft_capture.append(kwargs)
            return draft_response, [
                {"role": "user"},
                {"role": "assistant", "tool_calls": [{"id": "lookup"}]},
                {"role": "tool", "content": "evidence"},
                {"role": "assistant", "content": "closing draft"},
            ]

        async def synthesize(**_: Any) -> tuple[str, list[Any]]:
            return synthesis_response or json.dumps(make_generation_response("validated")), []

        monkeypatch.setattr(literature_tools, "get_mcp_client", get_client)
        monkeypatch.setattr(draft, "call_llm_with_tools", draft_call)
        monkeypatch.setattr(validate, "call_llm_with_tools", synthesis_call or synthesize)
        if novelty_call is not None:
            monkeypatch.setattr(validate, "call_llm_json", novelty_call)
        return await generate_node(
            make_state(
                generation_strategy="dev_isolation",
                initial_hypotheses_count=2,
                enable_tool_calling_generation=True,
                **{
                    "supervisor_guidance": {"key_areas": ["mechanism"]},
                    **(state_overrides or {}),
                },
            )
        )

    return run


async def test_unparseable_drafting_fails_generation(
    tools_node: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    with pytest.raises(ResponseParseError):
        await tools_node(draft_response="the agent emitted no JSON")


async def test_tool_generation_resolves_its_warm_literature_citations(
    tools_node: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    result = await tools_node(
        synthesis_response=json.dumps(
            make_generation_response("cited", literature_grounding="As shown in [C1].")
        ),
        state_overrides={"articles": [make_article(title="Smith 2020", used_in_analysis=True)]},
    )
    source = result["hypotheses"].items[0].citation_map["C1"]
    assert source["title"] == "Smith 2020"
    assert source["type"] == "paper"


async def test_a_transient_novelty_search_failure_is_retried_not_read_as_no_prior_art(
    monkeypatch: pytest.MonkeyPatch,
    tools_node: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    """A single failed call used to return no papers, which novelty
    validation cannot tell apart from an absence of prior art. The path now
    uses the literature-review caller, which retries first.
    """
    # The repo's retry idiom: zero the backoff, not asyncio.
    monkeypatch.setattr(search_query, "_search_retry_delay", lambda _attempt: 0.0)
    attempts: list[str] = []

    class _FlakyClient(FakeToolResultsClient):
        async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
            attempts.append(tool_name)
            raise RuntimeError("connection reset")

    await tools_node(client=_FlakyClient())

    assert len(attempts) > 1
