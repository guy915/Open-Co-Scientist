from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.metadata import version
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from co_scientist.platform.llm.request.backend import CompletionBackend

_MODELS = (
    "MODEL_NAME",
    "SUPERVISOR_MODEL_NAME",
    "CHAT_MODEL_NAME",
    "SEMANTIC_SAFETY_MODEL",
    "CLAIM_VERIFIER_MODEL",
)
_SENDING: ContextVar[bool] = ContextVar("paid_benchmark_sending", default=False)
POLICY_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def configure(provider: str, model: str) -> tuple[str, str]:
    if "co_scientist.core.config" in sys.modules:
        raise ValueError("paid benchmark requires a fresh process")
    if provider == "azure":
        # Its local ledger would be empty; Azure credit is admitted only by
        # the production API's recorded allowance.
        raise ValueError("paid benchmark refuses Azure; its credit is spent only by the API")
    if provider != "anthropic" or not re.fullmatch(rf"{provider}/[A-Za-z0-9_.-]+", model):
        raise ValueError("paid benchmark requires an explicit matching provider model")
    if os.getenv("COSCIENTIST_TEST_DOUBLE") or os.getenv("COSCIENTIST_FORCE_OFFLINE") == "1":
        raise ValueError("paid benchmark refuses deterministic execution")
    credentials = {"ANTHROPIC_API_KEY"}
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise ValueError("paid benchmark requires ANTHROPIC_API_KEY")
    base, version = "https://api.anthropic.com", ""
    if any(os.getenv(name) for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_BASE")):
        raise ValueError("paid benchmark requires the standard Anthropic endpoint")
    for name in list(os.environ):
        if name.upper().endswith("_API_KEY") and name not in credentials:
            del os.environ[name]
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS"] = "True"
    os.environ["COSCIENTIST_REQUIRE_FREE_MODELS"] = "0"
    os.environ.pop("COSCIENTIST_TEST_DOUBLE", None)
    for name in _MODELS:
        os.environ[name] = model
    endpoint_digest = hashlib.sha256(
        json.dumps({"base": base, "version": version}, sort_keys=True).encode()
    ).hexdigest()
    return str(urlsplit(base).hostname), endpoint_digest


class ProviderBackend:
    def __init__(self, delegate: CompletionBackend, model: str, host: str) -> None:
        self.delegate, self.model, self.host = delegate, model, host

    def supports_json_schema(self, model_name: str) -> bool:
        return self.delegate.supports_json_schema(model_name)

    async def complete(self, **completion_args: Any) -> Any:
        if completion_args.get("model") != self.model:
            raise ValueError("paid benchmark refuses an undeclared fallback model")
        base = completion_args.get("api_base")
        if base and urlsplit(str(base)).hostname != self.host:
            raise ValueError("paid benchmark refuses an undeclared provider endpoint")
        token = _SENDING.set(True)
        try:
            return await self.delegate.complete(**completion_args)
        finally:
            _SENDING.reset(token)


@contextmanager
def count_paid_attempts(host: str) -> Iterator[None]:
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    from evaluations import quality_benchmark

    counter: quality_benchmark.BoundedBackend | None = None

    class PaidBoundedBackend(quality_benchmark.BoundedBackend):
        def __init__(self, delegate: CompletionBackend, ceiling: int) -> None:
            nonlocal counter
            super().__init__(delegate, ceiling)
            if counter is not None:
                raise ValueError("paid benchmark requires one request counter")
            counter = self

        def reserve(self) -> None:
            from co_scientist.core.exceptions import LLMCallBudgetExceededError

            with self._lock:
                if self.calls >= self.ceiling:
                    raise LLMCallBudgetExceededError(self.calls + 1, self.ceiling)
                self.calls += 1

        async def complete(self, **completion_args: Any) -> Any:
            completion_args.update(num_retries=0, max_retries=0)
            return await self.delegate.complete(**completion_args)

    original = httpx.AsyncClient._send_single_request
    replay = AsyncHTTPHandler.single_connection_post_request

    async def refuse_replay(handler: Any, *values: Any, **options: Any) -> Any:
        if _SENDING.get():
            raise ValueError("paid benchmark refuses ambiguous SDK connection replay")
        return await replay(handler, *values, **options)

    async def send(client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
        if _SENDING.get() or request.url.host == host:
            if request.url.scheme != "https" or request.url.host != host:
                raise ValueError("paid benchmark refuses provider redirects to another host")
            if not isinstance(counter, PaidBoundedBackend):
                raise ValueError("paid provider request has no active physical counter")
            counter.reserve()
        return await original(client, request)

    # Native token-count preflights precede backend.complete; include them by host.
    with (
        patch.dict(os.environ, {"DISABLE_AIOHTTP_TRANSPORT": "True"}),
        patch.object(quality_benchmark, "BoundedBackend", PaidBoundedBackend),
        patch.object(httpx.AsyncClient, "_send_single_request", send),
        patch.object(AsyncHTTPHandler, "single_connection_post_request", refuse_replay),
    ):
        yield


def trusted_transport() -> ModuleType:
    path = Path(__file__).with_name("benchmark_transport.py")
    spec = importlib.util.spec_from_file_location("paid_benchmark_transport", path)
    if spec is None or spec.loader is None:
        raise ValueError("trusted benchmark transport is unavailable")
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    digest = hashlib.sha256(
        json.dumps(
            {
                "transport": vars(transport)["COUNTER_SHA256"],
                "paid_policy": POLICY_SHA256,
                "openai": version("openai"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    vars(transport)["COUNTER_SHA256"] = digest
    return transport


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit bounded paid paired benchmark.")
    parser.add_argument("operation", choices=("collect", "compare"))
    parser.add_argument("--provider", choices=("anthropic",), required=True)
    parser.add_argument("--model", default=os.getenv("MODEL_NAME", ""))
    args, remaining = parser.parse_known_args()
    required = "--live" if args.operation == "collect" else "--live-judge"
    if required not in remaining:
        parser.error("paid benchmark requires explicit live execution")
    host, endpoint_digest = configure(args.provider, args.model)
    # Select the clean research checkout, with the adapter kept in the trusted harness.
    sys.path.insert(0, str(Path.cwd()))
    from evaluations import _identity, _live_config

    original_identity = _identity.arm_identity

    def paid_identity(*values: Any, **options: Any) -> dict[str, Any]:
        identity = original_identity(*values, **options)
        identity["execution_environment"].update(
            COSCIENTIST_BENCHMARK_PROVIDER=args.provider,
            COSCIENTIST_BENCHMARK_POLICY_SHA256=POLICY_SHA256,
            COSCIENTIST_BENCHMARK_ENDPOINT_SHA256=endpoint_digest,
        )
        identity.pop("digest")
        identity["digest"] = _identity.identity_digest(identity)
        return identity

    def selected_model(selected: str | None = None) -> str:
        if selected not in (None, args.model):
            raise ValueError("paid benchmark model changed during execution")
        return str(args.model)

    from co_scientist.platform.llm import scoped_api_key
    from co_scientist.platform.llm.request.backend import active_backend, using_backend

    transport = trusted_transport()
    sys.argv = [sys.argv[0], args.operation, *remaining]
    key_name = "ANTHROPIC_API_KEY"
    with (
        patch.object(_live_config, "configure_live_environment", selected_model),
        patch.object(_identity, "arm_identity", paid_identity),
        patch.object(transport, "count_http_attempts", lambda: count_paid_attempts(host)),
        using_backend(ProviderBackend(active_backend(), args.model, host)),
        scoped_api_key(os.environ[key_name], {args.model: os.environ[key_name]}),
    ):
        result: int = transport.main()
        return result


if __name__ == "__main__":
    raise SystemExit(main())
