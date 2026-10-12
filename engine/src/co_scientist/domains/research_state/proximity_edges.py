from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Change method/version for new metrics rather than redefining stored judgments.
PROXIMITY_METHOD = "llm-cluster"

PROXIMITY_METHOD_VERSION = "1"


def is_judged_edge(edge: Mapping[str, Any]) -> bool:
    """Missing method denotes legacy model judgments; treating it as computed
    on resume would silently withdraw real evidence."""
    return bool(edge.get("method", PROXIMITY_METHOD) == PROXIMITY_METHOD)
