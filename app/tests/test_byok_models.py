from __future__ import annotations

from typing import Any

import pytest
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.core.run_modes import resolved_run_config
from co_scientist.domains.access import credentials
from co_scientist.orchestration.engine_adapter.opts import build_generator
from starlette.datastructures import Headers

from tests._store_helpers import seed_run

_KEY = "sk-model-choice-123456"


@pytest.fixture
def byok_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "byok_encryption_key", "model-choice")


def _headers(**extra: str) -> Headers:
    return Headers({"X-LLM-API-Key": _KEY, "X-LLM-Provider": "deepseek", **extra})


def test_a_model_from_another_provider_is_refused() -> None:
    with pytest.raises(credentials.ByokRequestError):
        credentials.credential_from_headers(_headers(**{"X-LLM-Model": "openai/gpt-4o"}))


def test_generator_runs_each_tier_on_its_chosen_model() -> None:
    captured: dict[str, Any] = {}

    def generator(**kwargs: Any) -> None:
        captured.update(kwargs)

    cred = byok_scope.ByokCredential(
        provider="deepseek",
        api_key=_KEY,
        model="deepseek/deepseek-v4-flash",
        supervisor_model="deepseek/deepseek-v4-pro",
    )
    build_generator(generator, resolved_run_config({}), byok=cred)
    assert captured["model_name"] == "deepseek/deepseek-v4-flash"
    options = captured["options"]
    assert options.supervisor_model_name == "deepseek/deepseek-v4-pro"


_SUPERVISOR_KEY = "sk-gemini-supervisor-987654"
_MIXED = {
    "X-LLM-Supervisor-Provider": "gemini",
    "X-LLM-Supervisor-API-Key": _SUPERVISOR_KEY,
    "X-LLM-Supervisor-Model": "gemini/gemini-3.1-pro-preview",
}


def _mixed_credential() -> byok_scope.ByokCredential:
    cred = credentials.credential_from_headers(_headers(**_MIXED))
    assert cred is not None
    return cred


def test_mixed_credential_round_trips_through_storage(
    byok_secret: None,
) -> None:
    run = seed_run("goal", profile="express")
    cred = _mixed_credential()
    credentials.store_run_credential(run.id, run.client_id, cred)
    assert credentials.get_run_credential(run.id) == cred


async def test_validation_probes_each_model_with_its_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, object]] = []

    async def fake_acompletion(**kwargs: object) -> None:
        seen.append((kwargs["model"], kwargs["api_key"]))

    monkeypatch.setattr(credentials, "_acompletion", fake_acompletion)
    await credentials.validate_byok_credential(_mixed_credential())
    assert seen == [
        ("deepseek/deepseek-flash", _KEY),
        ("gemini/gemini-3.1-pro-preview", _SUPERVISOR_KEY),
    ]
