import json
from typing import Any


def failed_sources_in(
    reviews: list[dict[str, Any]], calls: list[dict[str, Any]] | None = None
) -> list[str]:
    sources = {
        str(call["source"])
        for call in calls or []
        if call.get("status") == "failed" and call.get("source")
    }
    for review in reviews:
        try:
            detail = json.loads(review.get("detail_json") or "null")
        except (ValueError, TypeError):
            continue
        if not isinstance(detail, dict):
            continue
        for source in detail.get("failed_sources") or []:
            if isinstance(source, str):
                sources.add(source)
        for error in detail.get("retrieval_errors") or []:
            if isinstance(error, str):
                sources.add(error.split(":", 1)[0])
    return sorted(sources)
