from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from co_scientist.core.constants import corpus_slug
from co_scientist.platform.retrieval.config.registry import ToolRegistry
from co_scientist.platform.retrieval.config.schema import WorkflowConfig
from co_scientist.platform.retrieval.evidence.retrieval_support import (
    build_content_config,
    describe_exception,
    parse_content_result,
)
from co_scientist.platform.retrieval.evidence.search_query import (
    _build_query_tool_params,
    call_search_tool,
)
from co_scientist.platform.retrieval.evidence.search_support import (
    SearchConfig,
    normalize_search_response,
)
from co_scientist.platform.retrieval.mcp_client import MCPToolClient
from co_scientist.platform.retrieval.research import (
    ResearchBudget,
    RetrievalError,
    SourceHit,
)

logger = logging.getLogger(__name__)


# Exclude fetch-varying metadata from content identity so retries hash the same
# search.
_CARRIED_FIELDS = ("doi", "pmid", "year", "url", "publication", "venue")


@dataclass(frozen=True)
class ResearchRun:
    run_id: str
    research_goal: str = ""

    @property
    def slug(self) -> str:
        """Use the shared goal slug: alternate derivations silently miss the
        warm corpus.
        """
        return corpus_slug(self.research_goal) if self.research_goal else ""


class McpRetrieval:
    """Keep raw hit metadata per request because full-text tools need URLs
    absent from opaque locators.
    """

    def __init__(
        self,
        mcp_client: MCPToolClient,
        registry: ToolRegistry,
        workflow: WorkflowConfig,
        run: ResearchRun,
    ) -> None:
        self._client = mcp_client
        self._registry = registry
        self._workflow = workflow
        self._run = run
        self._records: dict[str, tuple[str, dict[str, Any]]] = {}
        self.sources: tuple[str, ...] = tuple(
            source.tool for source in workflow.get_enabled_search_sources()
        )

    @classmethod
    async def open_for(cls, config: SearchConfig, run_id: str) -> McpRetrieval:
        from co_scientist.platform.retrieval.mcp_client import get_mcp_client

        if config.tool_registry is None or config.workflow is None:
            raise ValueError("Research requires a tool registry and workflow")
        client = await get_mcp_client(tool_registry=config.tool_registry)
        return cls(
            client,
            config.tool_registry,
            config.workflow,
            ResearchRun(run_id=run_id, research_goal=config.research_goal),
        )

    async def search(self, *, query: str, source: str, limit: int) -> Sequence[SourceHit]:
        tool_config = self._registry.get_tool(source)
        if tool_config is None:
            raise RetrievalError(source, "no such tool in the registry")
        params = _build_query_tool_params(
            query, self._run.slug, self._run.run_id, limit, tool_config
        )
        try:
            raw = await call_search_tool(self._client, tool_config.mcp_tool_name, params)
        except Exception as exc:
            raise RetrievalError(source, describe_exception(exc)) from exc
        return self._to_hits(normalize_search_response(raw, tool_config), source, limit)

    def record(self, locator: str) -> dict[str, Any] | None:
        """Opaque locators omit titles, URLs and years; only the original
        retrieval retains source metadata.
        """
        known = self._records.get(locator)
        return dict(known[1]) if known is not None else None

    async def read(self, *, locator: str) -> str | None:
        """Failed or unavailable full text falls back to the snippet rather
        than dropping the document.
        """
        known = self._records.get(locator)
        if known is None:
            return None
        source, record = known
        config = build_content_config(
            self._workflow, self._registry, self._workflow.is_multi_source()
        ).get(source)
        if config is None:
            return None
        url = record.get(config.url_field)
        if not url:
            return None
        return await self._fetch(locator, str(url), config)

    async def _fetch(self, locator: str, url: str, config: Any) -> str | None:
        from co_scientist.platform.retrieval.config.schema import resolve_content_params

        params = resolve_content_params(
            config.content_params,
            {"research_goal": self._run.research_goal, "focus_areas": []},
        )
        try:
            result = await self._client.call_tool(config.mcp_tool_name, url=url, **params)
        except Exception as exc:
            logger.warning(
                "Could not read %s via %s: %s",
                locator,
                config.mcp_tool_name,
                describe_exception(exc),
            )
            return None
        return parse_content_result(result)

    def _to_hits(
        self,
        normalized: dict[str, dict[str, Any]],
        source: str,
        limit: int,
    ) -> list[SourceHit]:
        hits = []
        for rank, (locator, record) in enumerate(normalized.items()):
            if rank >= limit:
                break
            self._records[locator] = (source, record)
            hits.append(
                SourceHit(
                    locator=locator,
                    title=str(record.get("title") or locator),
                    snippet=str(record.get("abstract") or ""),
                    rank=rank,
                    score=_as_score(record.get("retrieval_score")),
                    metadata=_carried(record, source),
                )
            )
        return hits


def _as_score(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _carried(record: dict[str, Any], source: str) -> dict[str, str]:
    carried = {"source": source}
    for field in _CARRIED_FIELDS:
        value = record.get(field)
        if value not in (None, "", []):
            carried[field] = str(value)
    return carried


_TIER_CEILINGS: dict[str, tuple[int, int, int]] = {
    "extended": (2, 4, 4),
    "ultra": (3, 6, 5),
}

# Each research thread fans out into searches and model calls; bound concurrency
# before sources throttle.
_CONCURRENCY = 3


def _budget(ceilings: tuple[int, int, int] | None, sources: Sequence[str]) -> ResearchBudget | None:
    if ceilings is None or not sources:
        return None
    depth, breadth, hits = ceilings
    return ResearchBudget(
        depth=depth,
        breadth=breadth,
        concurrency=_CONCURRENCY,
        hits_per_question=hits,
        sources=tuple(sources),
    )


def budget_for_tier(tier: str, sources: Sequence[str]) -> ResearchBudget | None:
    """No sources or an unfunded tier means skip research, not a failed run."""
    return _budget(_TIER_CEILINGS.get(tier), sources)


def tier_researches(tier: str) -> bool:
    return tier in _TIER_CEILINGS


# Per-hypothesis budgets multiply across the pool, so they remain below run-
# level research.
_REVIEW_CEILINGS: dict[str, tuple[int, int, int]] = {
    "extended": (2, 2, 3),
    "ultra": (2, 3, 4),
}

# Review ceilings apply per cycle, not per run; total work also multiplies by
# max_iterations.
_REVIEW_HYPOTHESES: dict[str, int] = {
    "extended": 5,
    "ultra": 8,
}


def review_budget_for_tier(tier: str, sources: Sequence[str]) -> ResearchBudget | None:
    return _budget(_REVIEW_CEILINGS.get(tier), sources)


def reviewed_hypothesis_limit(tier: str) -> int:
    return _REVIEW_HYPOTHESES.get(tier, 0)


__all__ = [
    "LlmResearchModel",
    "McpRetrieval",
    "budget_for_tier",
    "tier_researches",
]
