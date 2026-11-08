from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from co_scientist.api.auth import client_id
from co_scientist.api.runs.models import CreateRunRequest
from co_scientist.core.async_bridge import off_loop
from co_scientist.core.run_modes import FREE_TIER_CREDITS
from co_scientist.domains.access.free_usage import FreeUsageExhaustedError, usage_payload

_NUMERIC_KNOBS = (
    "initial_hypotheses_count",
    "max_iterations",
    "evolution_max_count",
    "k_factor",
)

router = APIRouter(tags=["free-usage"])


def check_request(req: CreateRunRequest) -> None:
    """Numeric overrides cannot enlarge a free run beyond its tier's funded
    envelope.
    """
    if any(getattr(req, knob) is not None for knob in _NUMERIC_KNOBS):
        raise HTTPException(
            status_code=403,
            detail=(
                "Free runs use the standard settings of their run type. Add "
                "your own API key in Settings > Model to customize them."
            ),
        )


def exhausted_error(error: FreeUsageExhaustedError) -> HTTPException:
    if error.remaining < min(FREE_TIER_CREDITS.values()):
        return HTTPException(
            status_code=429,
            detail=(
                "You have used your free runs for today. Add your own API key "
                "in Settings > Model, or try again tomorrow (UTC)."
            ),
        )
    return HTTPException(
        status_code=429,
        detail=(
            f"A free {error.tier.capitalize()} run needs {error.needed} credits and "
            f"you have {error.remaining} left today. Pick a smaller run type, add "
            "your own API key in Settings > Model, or try again tomorrow (UTC)."
        ),
    )


@router.get("/api/free-usage")
@off_loop
def get_free_usage(request: Request) -> dict[str, Any]:
    """Return how many free credits the caller has left today."""
    return usage_payload(client_id(request))
