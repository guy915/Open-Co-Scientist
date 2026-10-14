from __future__ import annotations

import os
from typing import Any

from co_scientist.domains.access import custom_models
from co_scientist.main import app
from co_scientist.platform.llm.offline.llm import offline_acompletion
from co_scientist.platform.llm.request.backend import install_backend

if os.getenv("COSCIENTIST_TEST_DOUBLE") != "deterministic":
    raise RuntimeError("Browser harness requires the explicit deterministic test adapter")

_KEY = "synthetic-e2e-custom-key"
_MODELS = ("test/custom-worker", "test/custom-supervisor")


def fake_provider_models(provider: str, api_key: str) -> list[dict[str, Any]]:
    if provider != "openrouter" or api_key != _KEY:
        raise custom_models.ModelProviderError("The fake provider rejected this key")
    return [
        {
            "id": model,
            "context_length": 131072,
            "supported_parameters": ["tools", "structured_outputs"],
        }
        for model in _MODELS
    ]


class BrowserTestBackend:
    async def complete(self, **kwargs: Any) -> Any:
        model = str(kwargs["model"])
        if model in {f"openrouter/{name}" for name in _MODELS}:
            assert kwargs.get("api_key") == _KEY
            if (
                kwargs.get("tools")
                and kwargs["tools"][0]["function"]["name"] == "model_check"
            ):
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "model_check",
                                            "arguments": '{"ok":true}',
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            kwargs = {**kwargs, "model": "offline/browser-byok"}
        assert str(kwargs["model"]).startswith("offline/"), (
            "Browser tests cannot contact providers"
        )
        return await offline_acompletion(**kwargs)

    def supports_json_schema(self, model_name: str) -> bool:
        return True


# Only the browser harness loads this module; the production entry point has
# no synthetic model list, key, or provider backend.
custom_models._provider_models = fake_provider_models
install_backend(BrowserTestBackend())

__all__ = ["app"]
