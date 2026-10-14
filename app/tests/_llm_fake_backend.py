from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

_ENGINE_FAKE_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "engine" / "tests" / "_llm_fake.py"
)


def load_engine_fake() -> Any:
    spec = importlib.util.spec_from_file_location("engine_llm_fake", _ENGINE_FAKE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install_completion_backend(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[..., Any],
    *,
    requests: list[dict[str, Any]] | None = None,
    supports_json_schema: Callable[[str], bool] | None = None,
) -> Any:
    # Install the fake behind the offline router and preserve its JSON-schema
    # capability protocol.
    from co_scientist.platform.llm.offline.llm import OfflineRouter
    from co_scientist.platform.llm.request import backend

    engine_fake = load_engine_fake()
    fake = engine_fake.FakeBackend(
        respond, requests=requests, supports_json_schema=supports_json_schema
    )
    engine_fake.restore_backend_at_teardown(monkeypatch)
    backend.install_backend(OfflineRouter(fake))
    return fake


def completion_response(content: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
    )


def fake_completion(
    content: str,
) -> Callable[..., Awaitable[types.SimpleNamespace]]:
    async def completion(**_kwargs: Any) -> types.SimpleNamespace:
        return completion_response(content)

    return completion


def semantic_response(category: str) -> types.SimpleNamespace:
    return completion_response(f'{{"category":"{category}","reason":"model"}}')
