from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import unquote

import httpx

from co_scientist.core.byok_scope import ByokCredential, scoped_byok

_TIMEOUT = 15.0
_TOTAL_TIMEOUT = 30.0
_MAX_BYTES = 8 * 1024 * 1024
_MAX_ROWS = 10000
_MAX_PAGES = 20

_ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
    "deepseek": "https://api.deepseek.com/models",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models",
}


class ModelCatalogError(ValueError):
    pass


class ModelCatalogTimeoutError(ModelCatalogError):
    pass


def read_provider_models(provider: str, api_key: str) -> list[dict[str, Any]]:
    if provider not in _ENDPOINTS:
        raise ModelCatalogError("Unsupported provider")
    try:
        with scoped_byok(ByokCredential(provider, api_key, f"{provider}/catalog")):
            return asyncio.run(
                asyncio.wait_for(_read_provider_models(provider, api_key), timeout=_TOTAL_TIMEOUT)
            )
    except ModelCatalogTimeoutError:
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise ModelCatalogTimeoutError("The provider timed out") from None
    except Exception:
        # Provider errors can include request headers or echo credentials.
        raise ModelCatalogError("The provider could not return a model list") from None


async def _read_provider_models(provider: str, api_key: str) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif provider == "gemini":
        headers = {"x-goog-api-key": api_key}
    # Refuse unexpected compression before decoding can expand a small chunk.
    headers["Accept-Encoding"] = "identity"
    models: list[dict[str, Any]] = []
    params: dict[str, str] = {}
    received = 0
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=False, trust_env=False
    ) as client:
        for _ in range(_MAX_PAGES):
            async with client.stream(
                "GET", _ENDPOINTS[provider], headers=headers, params=params
            ) as response:
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ModelCatalogError("The provider returned an encoded model list")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > _MAX_BYTES - received:
                    raise ModelCatalogError("The provider model list exceeded the validation limit")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    received += len(chunk)
                    if received > _MAX_BYTES:
                        raise ModelCatalogError(
                            "The provider model list exceeded the validation limit"
                        )
                    body.extend(chunk)
            payload = json.loads(body)
            rows = payload.get("models" if provider == "gemini" else "data")
            if not isinstance(rows, list) or len(models) + len(rows) > _MAX_ROWS:
                raise ModelCatalogError("The provider returned an invalid model list")
            models.extend(row for row in rows if isinstance(row, dict))
            if provider == "gemini" and payload.get("nextPageToken"):
                params = {"pageToken": str(payload["nextPageToken"])}
            elif provider == "anthropic" and payload.get("has_more") and payload.get("last_id"):
                params = {"after_id": str(payload["last_id"])}
            else:
                return models
            token = next(iter(params.values()))
            if len(token) > 2048 or (api_key and api_key in unquote(token)):
                raise ModelCatalogError("The provider returned an invalid pagination token")
    raise ModelCatalogError("The provider model list exceeded the validation limit")
