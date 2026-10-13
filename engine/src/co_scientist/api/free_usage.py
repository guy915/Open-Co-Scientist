from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from co_scientist.api.auth import client_id
from co_scientist.api.runs.models import CreateRunRequest
from co_scientist.core.async_bridge import off_loop
from co_scientist.domains.access.free_usage import FREE_TIER, daily_limit, usage_payload

_NUMERIC_KNOBS = (
    "initial_hypotheses_count",
    "max_iterations",
    "evolution_max_count",
    "k_factor",
)

router = APIRouter(tags=["free-usage"])


def check_request(req: CreateRunRequest, tier: str) -> None:
    """Numeric overrides cannot enlarge a free express run beyond its funded
    envelope.
    """
    if tier != FREE_TIER or any(getattr(req, knob) is not None for knob in _NUMERIC_KNOBS):
        raise HTTPException(
            status_code=403,
            detail=(
                "Free usage is limited to Express runs. Add your own API "
                "key in Settings > Model to use other run types."
            ),
        )


def exhausted_error() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail=(
            f"You have used your {daily_limit()} free runs for today. "
            "Add your own API key in Settings > Model, or try again "
            "tomorrow (UTC)."
        ),
    )


@router.get("/api/free-usage")
@off_loop
def get_free_usage(request: Request) -> dict[str, Any]:
    """Return how many free runs the caller has left today."""
    return usage_payload(client_id(request))
