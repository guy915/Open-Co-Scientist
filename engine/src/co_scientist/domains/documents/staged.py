from __future__ import annotations

from typing import Any


def document_summary(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": document["id"],
        "title": document["title"],
        "mime_type": document["mime_type"],
        "byte_size": document["byte_size"],
    }
