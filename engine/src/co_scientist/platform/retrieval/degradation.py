"""MCP outages leave only in-process attached documents; record that loss
instead of implying grounding.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

CAPABILITIES_LOST_WITHOUT_MCP: tuple[str, ...] = (
    "literature_review",
    "observation_review",
    "deep_research",
    "review_evidence",
    "verification_probes",
    "evolution_grounding",
)

# Machine reason codes are rendered by the app, not quoted from logs.
MCP_UNREACHABLE = "mcp_unreachable"

FLOOR_NONE = "none"
FLOOR_RUN_ATTACHMENTS = "run_attachments"


def resolve_retrieval_degradation(
    *,
    mcp_available: bool,
    private_sources: Sequence[Any] | None,
) -> dict[str, Any] | None:
    if mcp_available:
        return None
    return {
        "reason": MCP_UNREACHABLE,
        "lost": list(CAPABILITIES_LOST_WITHOUT_MCP),
        "floor": FLOOR_RUN_ATTACHMENTS if private_sources else FLOOR_NONE,
    }
