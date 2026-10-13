from __future__ import annotations

import asyncio
import json
import re
import secrets
import threading
from dataclasses import dataclass
from time import monotonic
from typing import Any

from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.cmac import CMAC

from co_scientist.core.async_bridge import run_coroutine_sync
from co_scientist.core.byok_scope import ByokCredential, CustomModelCapabilities, scoped_byok
from co_scientist.domains.access.byok_models import ByokModelError
from co_scientist.platform.llm import ModelCatalogTimeoutError, read_provider_models

_TIMEOUT = 15.0
_CACHE_TTL = 120.0
_MIN_CONTEXT = 32768
_CACHE_SECRET = secrets.token_bytes(32)
_CACHE_LOCK = threading.Lock()


class ModelProviderError(ByokModelError):
    pass


class ModelProviderTimeoutError(ModelProviderError):
    pass


@dataclass(frozen=True)
class ModelValidation:
    model: str
    exists: bool
    supported: bool
    capabilities: CustomModelCapabilities | None = None
    error: str | None = None


_CACHE: dict[tuple[str, str, str], tuple[float, ModelValidation]] = {}


def normalize_model_id(provider: str, requested: str) -> str:
    if provider not in {"openrouter", "openai", "anthropic", "deepseek", "gemini"}:
        raise ByokModelError("unsupported BYOK provider")
    raw = requested.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", raw):
        raise ByokModelError("Enter a valid model ID")
    prefix = raw.split("/", 1)[0]
    if (
        provider != "openrouter"
        and prefix in (*("openrouter", "openai", "anthropic", "deepseek", "gemini"), "azure")
        and prefix != provider
    ):
        raise ByokModelError("Model not found for this provider")
    if raw.startswith("models/") and provider == "gemini":
        raw = raw.removeprefix("models/")
    return raw if raw.startswith(provider + "/") else f"{provider}/{raw}"


def _cache_key(provider: str, model: str, api_key: str) -> tuple[str, str, str]:
    # A per-process authenticator scopes admission to the exact key without
    # retaining the credential in the cache.
    authenticator = CMAC(AES(_CACHE_SECRET))
    authenticator.update(b"coscientist-custom-model-cache-v1\0" + api_key.encode())
    return provider, model, authenticator.finalize().hex()


def cached_validation(provider: str, model: str, api_key: str) -> ModelValidation | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(_cache_key(provider, model, api_key))
        if entry and entry[0] > monotonic():
            return entry[1]
    return None


def _provider_models(provider: str, api_key: str) -> list[dict[str, Any]]:
    return read_provider_models(provider, api_key)


def _capabilities(row: dict[str, Any]) -> CustomModelCapabilities:
    parameters = row.get("supported_parameters")
    params = parameters if isinstance(parameters, list) else []
    declared = row.get("capabilities")
    caps = declared if isinstance(declared, dict) else {}
    context = row.get("context_length", row.get("inputTokenLimit"))
    if not isinstance(context, int) or isinstance(context, bool) or context <= 0:
        context = None
    return CustomModelCapabilities(
        context_length=context,
        tool_calling="tools" in params
        if isinstance(parameters, list)
        else caps.get("tool_calling", True) is True,
        json_schema="structured_outputs" in params or caps.get("structured_outputs") is True,
        json_object="response_format" in params or caps.get("json_object") is True,
        reasoning="reasoning" in params or caps.get("reasoning") is True,
        reasoning_can_disable=caps.get("reasoning_can_disable") is True,
    )


async def _probe(provider: str, model: str, api_key: str, caps: CustomModelCapabilities) -> None:
    from co_scientist.domains.access.credentials import _acompletion
    from co_scientist.platform.llm.llm_scope import app_call_scope

    credential = ByokCredential(provider, api_key, model, custom_models={model: caps})
    with scoped_byok(credential), app_call_scope("custom_model_probe"):
        result = await asyncio.wait_for(
            _acompletion(
                model=model,
                api_key=api_key,
                messages=[{"role": "user", "content": "Call model_check with ok=true."}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "model_check",
                            "description": "Check tool calling",
                            "parameters": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                tool_choice={"type": "function", "function": {"name": "model_check"}},
                max_tokens=256,
                timeout=_TIMEOUT,
            ),
            timeout=_TIMEOUT,
        )
        # A successful HTTP response alone does not establish tool support.
        choices = (
            result.get("choices") if isinstance(result, dict) else getattr(result, "choices", None)
        )
        message = (
            choices[0].get("message")
            if choices and isinstance(choices[0], dict)
            else getattr(choices[0], "message", None)
            if choices
            else None
        )
        calls = (
            message.get("tool_calls")
            if isinstance(message, dict)
            else getattr(message, "tool_calls", None)
        )
        if not calls:
            raise ByokModelError(
                "This model does not support tool calling, which Co-Scientist needs"
            )
        function = (
            calls[0].get("function")
            if isinstance(calls[0], dict)
            else getattr(calls[0], "function", None)
        )
        name = (
            function.get("name") if isinstance(function, dict) else getattr(function, "name", None)
        )
        arguments = (
            function.get("arguments")
            if isinstance(function, dict)
            else getattr(function, "arguments", "")
        )
        if (
            name != "model_check"
            or not isinstance(arguments, str)
            or json.loads(arguments) != {"ok": True}
        ):
            raise ByokModelError("The model did not return a valid structured tool response")


def validate_custom_model(provider: str, requested: str, api_key: str) -> ModelValidation:
    model = normalize_model_id(provider, requested)
    if not api_key.strip():
        raise ByokModelError("Custom models require your own API key")
    cached = cached_validation(provider, model, api_key)
    if cached is not None:
        return cached
    try:
        # One total deadline bounds pagination and the tool probe together.
        async def check() -> ModelValidation:
            rows = await asyncio.to_thread(_provider_models, provider, api_key)
            native = model.removeprefix(provider + "/")
            row = next(
                (
                    row
                    for row in rows
                    if str(row.get("id", row.get("name", ""))).removeprefix("models/") == native
                ),
                None,
            )
            if row is None:
                return ModelValidation(
                    model, False, False, error="Model not found for this provider"
                )
            caps = _capabilities(row)
            if not caps.tool_calling:
                return ModelValidation(
                    model,
                    True,
                    False,
                    caps,
                    "This model does not support tool calling, which Co-Scientist needs",
                )
            if caps.context_length is not None and caps.context_length < _MIN_CONTEXT:
                return ModelValidation(
                    model,
                    True,
                    False,
                    caps,
                    f"This model needs a context length of at least {_MIN_CONTEXT} tokens",
                )
            try:
                await _probe(provider, model, api_key, caps)
            except ByokModelError as exc:
                return ModelValidation(model, True, False, caps, str(exc))
            return ModelValidation(model, True, True, caps)

        async def bounded_check() -> ModelValidation:
            return await asyncio.wait_for(check(), timeout=30.0)

        result = run_coroutine_sync(bounded_check)
    except (TimeoutError, ModelCatalogTimeoutError):
        raise ModelProviderTimeoutError(
            "The provider timed out; try checking the model again"
        ) from None
    except ModelProviderError:
        raise
    except Exception:
        # Provider exceptions can contain the key or an echoed request body.
        raise ModelProviderError("The provider could not verify this model with your key") from None
    with _CACHE_LOCK:
        now = monotonic()
        for key, (expires, _) in list(_CACHE.items()):
            if expires <= now:
                del _CACHE[key]
        if len(_CACHE) >= 512:
            del _CACHE[next(iter(_CACHE))]
        _CACHE[_cache_key(provider, model, api_key)] = now + _CACHE_TTL, result
    return result
