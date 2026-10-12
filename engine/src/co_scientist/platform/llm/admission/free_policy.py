import asyncio
import contextlib
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

import httpx
import litellm

from co_scientist.core._context import _bind_contextvar
from co_scientist.core.exceptions import FreeModelEligibilityError
from co_scientist.platform.llm.profile import is_free_route

_byok_api_key: ContextVar[str | None] = ContextVar("byok_api_key", default=None)
_byok_keys_by_model: ContextVar[Mapping[str, str]] = ContextVar(
    "byok_keys_by_model", default=MappingProxyType({})
)


def current_api_key() -> str | None:
    return _byok_api_key.get()


def api_key_for_model(model_name: str) -> str | None:
    """A run mixing providers bills each model to its own provider's key."""
    return _byok_keys_by_model.get().get(model_name) or _byok_api_key.get()


@contextlib.contextmanager
def scoped_api_key(
    api_key: str | None, by_model: Mapping[str, str] | None = None
) -> Iterator[None]:
    """None preserves the ambient task credential; explicit scopes restore it
    on every exit. An explicit key without a mapping shadows an ambient one.
    """
    if api_key is None:
        yield
        return
    with (
        _bind_contextvar(_byok_api_key, api_key),
        _bind_contextvar(_byok_keys_by_model, dict(by_model or {})),
    ):
        yield


FREE_MODE_ENV = "COSCIENTIST_REQUIRE_FREE_MODELS"
_API_BASE = "https://openrouter.ai/api/v1"
_REQUEST_FIELDS = {
    "model",
    "messages",
    "max_tokens",
    "temperature",
    "drop_params",
    "timeout",
    "api_key",
    "api_base",
    "extra_body",
    "response_format",
    "reasoning_effort",
    "tools",
    "tool_choice",
    "stream",
    "stream_options",
}
_BODY_FIELDS = {"provider", "models", "reasoning"}
_zero_cost_only: ContextVar[bool] = ContextVar("zero_cost_only", default=False)


CATALOG_URL = "https://openrouter.ai/api/v1/models"
CATALOG_TTL_SECONDS = 60


class CatalogReader:
    """Each reader owns its prices and threading lock across worker loops.
    Replacing a reader cannot inherit its predecessor's snapshot.
    """

    def __init__(self, loader: Callable[[], dict[str, Any]] | None = None) -> None:
        self._loader = loader if loader is not None else _fetch_catalog
        self._lock = threading.Lock()
        self._snapshot: tuple[float, dict[str, Any]] | None = None

    def read(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot is not None and time.monotonic() < self._snapshot[0]:
                return self._snapshot[1]
            self._snapshot = None
            catalog = self._loader()
            self._snapshot = (time.monotonic() + CATALOG_TTL_SECONDS, catalog)
            return catalog

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None


def _fetch_catalog() -> dict[str, Any]:
    try:
        response = httpx.get(CATALOG_URL, timeout=15, trust_env=False)
        response.raise_for_status()
        rows = response.json()["data"]
        catalog = {row["id"]: row for row in rows}
        if not rows or len(catalog) != len(rows):
            raise ValueError("empty or duplicate model entries")
        return catalog
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise FreeModelEligibilityError("zero-cost catalog unavailable or invalid") from exc


# The shared metadata reader must cross worker threads; API-loop ContextVars are
# invisible there.
_reader = CatalogReader()


def current_catalog() -> dict[str, Any]:
    """Never admit using stale prices when refresh fails."""
    return _reader.read()


def _is_zero(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        number = Decimal(value)
        return number.is_finite() and number == 0
    except InvalidOperation:
        return False


def verify_model(model: str, catalog: dict[str, Any]) -> None:
    row = catalog.get(model)
    if not isinstance(row, dict) or model.startswith(("openrouter/", "~")):
        raise FreeModelEligibilityError("zero-cost model is not an explicit catalog route")
    _verify_expiration(row)
    architecture = row.get("architecture", {})
    if not isinstance(architecture, dict):
        raise FreeModelEligibilityError("zero-cost route architecture is invalid")
    inputs = architecture.get("input_modalities")
    if not isinstance(inputs, list) or "text" not in inputs:
        raise FreeModelEligibilityError("zero-cost route cannot accept text")
    if architecture.get("output_modalities") != ["text"]:
        raise FreeModelEligibilityError("zero-cost route must produce only text")
    _verify_pricing(model, row.get("pricing"))


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _verify_expiration(row: dict[str, Any]) -> None:
    if "expiration_date" not in row or row["expiration_date"] is None:
        return
    value = row["expiration_date"]
    try:
        expiration = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        expiration = None
    if expiration is None or expiration.isoformat() != value:
        raise FreeModelEligibilityError("zero-cost route expiration date is invalid")
    # Catalog expiry is the last valid UTC date, not the first expired day.
    if expiration < _utc_today():
        raise FreeModelEligibilityError("zero-cost route expiration date has elapsed")


def _verify_pricing(model: str, pricing: Any) -> None:
    if not isinstance(pricing, dict):
        raise FreeModelEligibilityError("zero-cost pricing is missing")
    # Absent ancillary rates prove no zero price except on ":free" variants
    # and profile-declared free trials; max_price still caps the request.
    ancillary = {
        "request",
        "internal_reasoning",
        "input_cache_read",
        "input_cache_write",
    }
    required = {"prompt", "completion"} | (
        set() if is_free_route(f"openrouter/{model}") else ancillary
    )
    if not required <= pricing.keys():
        raise FreeModelEligibilityError("zero-cost pricing is incomplete")
    if pricing.get("overrides", []) != []:
        raise FreeModelEligibilityError("zero-cost conditional pricing is not qualified")
    rates = dict(pricing)
    rates.pop("overrides", None)
    if not all(_is_zero(value) for value in rates.values()):
        raise FreeModelEligibilityError("zero-cost route has paid or invalid pricing")


def free_models_required() -> bool:
    configured = os.getenv(FREE_MODE_ENV, "0").strip().lower()
    if configured not in {"0", "false", "", "1", "true"}:
        raise FreeModelEligibilityError("zero-cost mode setting is invalid")
    return configured in {"1", "true"}


@contextlib.contextmanager
def scoped_zero_cost_admission(enabled: bool) -> Iterator[None]:
    """Nested scopes may strengthen zero-cost admission but never weaken it."""
    with _bind_contextvar(_zero_cost_only, _zero_cost_only.get() or enabled):
        yield


def _requires_free(args: dict[str, Any], byok: bool) -> bool:
    model = str(args.get("model", ""))
    return free_models_required() or _zero_cost_only.get() or (not byok and is_free_route(model))


def _request_body(args: dict[str, Any]) -> dict[str, Any]:
    if litellm.model_fallbacks or litellm.model_alias_map:
        raise FreeModelEligibilityError("zero-cost SDK routing overrides are unqualified")
    if args.keys() - _REQUEST_FIELDS:
        raise FreeModelEligibilityError("zero-cost request contains unqualified options")
    if args.get("api_base", _API_BASE) != _API_BASE:
        raise FreeModelEligibilityError("zero-cost request uses an unverified endpoint")
    body = args.get("extra_body", {})
    if not isinstance(body, dict) or body.keys() - _BODY_FIELDS:
        raise FreeModelEligibilityError("zero-cost request contains plugins or unqualified routing")
    _verify_messages(args.get("messages", []))
    _verify_tools(args.get("tools", []))
    return body


def _object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise FreeModelEligibilityError("zero-cost request requires a list of objects")
    return value


def _verify_messages(messages: Any) -> None:
    for message in _object_list(messages):
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise FreeModelEligibilityError("zero-cost request contains non-text input")
        if message.keys() - {
            "role",
            "content",
            "tool_calls",
            "tool_call_id",
            "name",
            "reasoning_content",
            "reasoning",
            "reasoning_details",
        }:
            raise FreeModelEligibilityError("zero-cost message contains unqualified fields")


def _verify_tools(tools: Any) -> None:
    for tool in _object_list(tools):
        if tool.get("type") != "function" or set(tool) != {"type", "function"}:
            raise FreeModelEligibilityError("zero-cost request contains a server tool")


def _routes(args: dict[str, Any], body: dict[str, Any]) -> list[str]:
    primary = args.get("model", "")
    if not isinstance(primary, str) or not primary.startswith("openrouter/"):
        raise FreeModelEligibilityError("zero-cost requests require OpenRouter")
    fallbacks = body.get("models", [])
    if not isinstance(fallbacks, list) or not all(isinstance(m, str) for m in fallbacks):
        raise FreeModelEligibilityError("zero-cost fallback list is invalid")
    return [primary.removeprefix("openrouter/"), *fallbacks]


async def enforce_free_request(args: dict[str, Any], *, byok: bool = False) -> bool:
    """Process-wide free-only mode overrides BYOK; a deployment key is not
    evidence of caller-owned credentials.
    """
    if not _requires_free(args, byok):
        return False
    body = _request_body(args)
    routes = _routes(args, body)
    catalog = await asyncio.to_thread(current_catalog)
    for model in routes:
        verify_model(model, catalog)
    raw_provider = body.get("provider", {})
    if not isinstance(raw_provider, dict):
        raise FreeModelEligibilityError("zero-cost provider options must be an object")
    provider = dict(raw_provider)
    provider["max_price"] = {
        "prompt": 0,
        "completion": 0,
        "request": 0,
    }
    provider["require_parameters"] = True
    args["extra_body"] = {**body, "provider": provider}
    # Pin transport so environment proxy/base overrides cannot change the
    # billing service.
    args["api_base"] = _API_BASE
    return True
