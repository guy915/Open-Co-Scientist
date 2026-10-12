from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import httpx
import pytest
from co_scientist.core.byok_scope import CustomModelCapabilities
from co_scientist.core.config import settings
from co_scientist.domains.access import credentials, custom_models
from co_scientist.domains.access.byok_models import ByokModelError, resolve_model_choice
from co_scientist.main import app
from co_scientist.platform.llm import ModelCatalogTimeoutError, model_catalog
from fastapi.testclient import TestClient
from httpx2 import Response as ClientResponse
from starlette.datastructures import Headers

from tests._store_helpers import seed_run

_KEY = "synthetic-custom-key"
_MODEL = "openrouter/vendor/new-model"
_HEADERS = {
    "X-Client-ID": "custom-model-test",
    "X-LLM-Provider": "openrouter",
    "X-LLM-API-Key": _KEY,
}
_ROW = {
    "id": "vendor/new-model",
    "context_length": 131072,
    "supported_parameters": ["tools", "structured_outputs", "response_format", "reasoning"],
}


@pytest.fixture(autouse=True)
def fake_models(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    custom_models._CACHE.clear()
    rows: list[dict[str, Any]] = [_ROW.copy()]
    monkeypatch.setattr(custom_models, "_provider_models", lambda provider, key: rows)

    async def probe(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["api_key"] == _KEY
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "model_check", "arguments": '{"ok":true}'}}
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr(credentials, "_acompletion", probe)
    return rows


def validate(model: str = _MODEL, headers: dict[str, str] | None = None) -> ClientResponse:
    return TestClient(app).post(
        "/api/byok-models/validate",
        headers=_HEADERS if headers is None else headers,
        json={"provider": "openrouter", "model": model},
    )


def test_endpoint_exists_and_works_with_the_key() -> None:
    response = validate("vendor/new-model")
    assert response.status_code == 200
    assert response.json()["model"] == _MODEL
    assert response.json()["exists"] is True
    assert response.json()["supported"] is True
    assert response.json()["capabilities"] == asdict(
        CustomModelCapabilities(131072, True, True, True, True)
    )
    assert _KEY not in response.text


def test_endpoint_missing() -> None:
    response = validate("vendor/missing")
    assert response.status_code == 200
    assert response.json()["exists"] is False
    assert response.json()["error"] == "Model not found for this provider"


def test_endpoint_unsupported(fake_models: list[dict[str, Any]]) -> None:
    fake_models[0]["supported_parameters"] = ["reasoning"]
    response = validate()
    assert response.json()["exists"] is True
    assert response.json()["supported"] is False
    assert "does not support tool calling" in response.json()["error"]


def test_endpoint_small_context(fake_models: list[dict[str, Any]]) -> None:
    fake_models[0]["context_length"] = 8192
    assert "context length" in validate().json()["error"]


@pytest.mark.parametrize(
    "failure,status",
    [(httpx.ConnectError("oops " + _KEY), 502), (ModelCatalogTimeoutError(_KEY), 504)],
)
def test_endpoint_provider_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, status: int
) -> None:
    def refused(provider: str, key: str) -> list[dict[str, Any]]:
        raise failure

    monkeypatch.setattr(custom_models, "_provider_models", refused)
    response = validate()
    assert response.status_code == status
    assert _KEY not in response.text
    assert not custom_models._CACHE


def test_http_success_without_tool_response_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_tools(**kwargs: Any) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(credentials, "_acompletion", no_tools)
    assert validate().json()["supported"] is False


def test_custom_admission_requires_byok() -> None:
    assert validate(headers={}).status_code == 400
    with pytest.raises(ByokModelError, match="own API key"):
        resolve_model_choice("openrouter", _MODEL)
    with pytest.raises(credentials.ByokRequestError, match="own API key"):
        credentials.credential_from_headers(Headers({"X-LLM-Model": _MODEL}))
    assert validate(headers={**_HEADERS, "X-LLM-Provider": "azure"}).status_code == 400


def test_cache_is_short_lived_and_scoped_to_key(monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate().json()["supported"] is True
    assert custom_models.cached_validation("openrouter", _MODEL, _KEY) is not None
    assert custom_models.cached_validation("openrouter", _MODEL, "another-key") is None
    assert _KEY not in repr(custom_models._CACHE)
    monkeypatch.setattr("co_scientist.domains.access.custom_models.monotonic", lambda: 10**12)
    assert custom_models.cached_validation("openrouter", _MODEL, _KEY) is None


def test_custom_metadata_and_exact_model_survive_durable_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "byok_encryption_key", "custom-model-encryption-test")
    run = seed_run("goal", profile="express")
    credential = credentials.credential_from_headers(
        Headers({**_HEADERS, "X-LLM-Model": "vendor/new-model"})
    )
    assert credential is not None
    assert credential.model == _MODEL
    assert credential.custom_models[_MODEL].context_length == 131072
    credentials.store_run_credential(run.id, run.client_id, credential)
    custom_models._CACHE.clear()
    assert credentials.get_run_credential(run.id) == credential


@pytest.mark.parametrize(
    "provider,row",
    [
        ("openai", {"id": "new-model"}),
        ("anthropic", {"id": "new-model"}),
        ("deepseek", {"id": "new-model"}),
        ("gemini", {"name": "models/new-model", "inputTokenLimit": 131072}),
    ],
)
def test_other_provider_lists_and_conservative_capabilities(
    monkeypatch: pytest.MonkeyPatch, provider: str, row: dict[str, Any]
) -> None:
    monkeypatch.setattr(custom_models, "_provider_models", lambda provider, key: [row])
    result = custom_models.validate_custom_model(provider, "new-model", _KEY)
    assert result.supported
    assert result.capabilities is not None
    assert result.capabilities.json_schema is False


@pytest.mark.parametrize("provider", ["openrouter", "openai", "anthropic", "deepseek", "gemini"])
def test_list_requests_use_fixed_provider_host_and_header_credentials(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    seen: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            stream=httpx.ByteStream(
                json.dumps({"models" if provider == "gemini" else "data": []}).encode()
            ),
        )

    monkeypatch.setattr(
        "co_scientist.platform.llm.model_catalog.httpx.AsyncClient",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    assert model_catalog.read_provider_models(provider, _KEY) == []
    assert _KEY not in str(seen[0].url)
    assert _KEY in seen[0].headers.values() or f"Bearer {_KEY}" in seen[0].headers.values()


def test_probe_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def stalled(**kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(custom_models, "_TIMEOUT", 0.01)
    monkeypatch.setattr(credentials, "_acompletion", stalled)
    assert validate().status_code == 504
    assert not custom_models._CACHE


def test_failed_validation_never_admits_a_custom_choice(fake_models: list[dict[str, Any]]) -> None:
    fake_models.clear()
    with pytest.raises(ByokModelError, match="not found"):
        resolve_model_choice("openrouter", _MODEL, api_key=_KEY)
