from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from co_scientist.api.operator_access import is_operator
from co_scientist.core.async_bridge import off_loop
from co_scientist.platform.db import connect, current_time
from co_scientist.platform.db.usage_view import usage_snapshot

router = APIRouter(tags=["operations"])


@router.get("/api/usage")
@off_loop
def get_usage(
    request: Request,
    response: Response,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
) -> dict[str, Any]:
    """Distinct browsers, chats and runs per UTC day, for the operator only.

    A browser is one anonymous ownership ID, so a person on two devices
    counts twice and cleared storage starts a new one.
    """
    if not is_operator(request):
        raise HTTPException(404, "not found")
    response.headers["Cache-Control"] = "no-store"
    with connect() as conn:
        conn.execute("BEGIN")
        return usage_snapshot(conn, now=current_time(), days=days)
