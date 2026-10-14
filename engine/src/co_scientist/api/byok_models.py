from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from co_scientist.core.async_bridge import off_loop
from co_scientist.domains.access.byok_models import ByokModelError, model_catalog
from co_scientist.domains.access.credentials import API_KEY_HEADER, PROVIDER_HEADER
from co_scientist.domains.access.custom_models import (
    ModelProviderError,
    ModelProviderTimeoutError,
    validate_custom_model,
)

router = APIRouter(tags=["byok-models"])


@router.get("/api/byok-models")
@off_loop
def get_byok_models() -> dict[str, dict[str, list[str]]]:
    """Return the models each BYOK provider offers, default first."""
    return {"providers": model_catalog()}


class ValidateModelRequest(BaseModel):
    provider: str = Field(max_length=32)
    model: str = Field(min_length=1, max_length=200)


@router.post("/api/byok-models/validate")
@off_loop
def validate_model(body: ValidateModelRequest, request: Request) -> dict[str, object]:
    provider = body.provider.strip().lower()
    header_provider = (request.headers.get(PROVIDER_HEADER) or "").strip().lower()
    key = (request.headers.get(API_KEY_HEADER) or "").strip()
    if not key or header_provider != provider:
        raise HTTPException(400, "Custom models require your own key for this provider")
    try:
        return asdict(validate_custom_model(provider, body.model, key))
    except ModelProviderTimeoutError as exc:
        raise HTTPException(504, str(exc)) from None
    except ModelProviderError as exc:
        raise HTTPException(502, str(exc)) from None
    except ByokModelError as exc:
        raise HTTPException(400, str(exc)) from None
