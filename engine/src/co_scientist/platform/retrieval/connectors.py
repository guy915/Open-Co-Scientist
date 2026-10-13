from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)


_KNOWN_CONNECTORS: tuple[tuple[str, str], ...] = (
    ("web_search", "Web search"),
    ("pubmed", "PubMed"),
    ("arxiv", "arXiv"),
    ("biorxiv", "BioRxiv"),
)


def connectors_report(
    *,
    literature_available: bool,
    enabled_tools: list[str] | None,
    web_search_available: bool = False,
) -> list[dict[str, str]]:
    """Advertised sources require actual reachability and configuration;
    shared-MCP keyless sources require both.
    """
    tool_blob = " ".join(enabled_tools or []).lower()
    available_by_probe = {
        "pubmed": literature_available,
        "web_search": web_search_available,
        "arxiv": literature_available and "arxiv" in tool_blob,
        "biorxiv": literature_available and "biorxiv" in tool_blob,
    }
    connectors: list[dict[str, str]] = []
    for key, display in _KNOWN_CONNECTORS:
        probed = available_by_probe.get(key)
        listed = probed if probed is not None else key in tool_blob
        if listed:
            connectors.append({"id": key, "display": display})
    if not connectors:
        connectors.append({"id": "pubmed", "display": "PubMed"})
    return connectors


@functools.lru_cache(maxsize=1)
def enabled_tools() -> list[str] | None:
    """Process-lifetime caching avoids reparsing YAML on every status poll."""
    try:
        from co_scientist.platform.retrieval.config import ToolRegistry
    except Exception:  # pragma: no cover - engine optional at runtime
        return None
    try:
        registry = ToolRegistry()
        return sorted(registry.get_enabled_tools().keys())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not enumerate tools: %s", exc)
        return None
