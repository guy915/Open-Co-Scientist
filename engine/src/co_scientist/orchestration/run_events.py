from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from co_scientist.domains.research_state.text_utils import hypothesis_id, hypothesis_title
from co_scientist.orchestration.repository import events as store

EmitFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def make_emitter(
    run_id: str,
    *,
    db_path: str | None = None,
) -> EmitFn:

    async def emit(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Persisted monotonic per-run sequences anchor SSE replay and
        # reconnection.
        seq = store.append_event(run_id, type_, payload, db_path=db_path)
        return {"seq": seq, "type": type_, "payload": payload}

    return emit


def hypothesis_stub(h: dict[str, Any]) -> dict[str, str]:
    return {
        "id": hypothesis_id(h),
        "title": hypothesis_title(h),
    }
