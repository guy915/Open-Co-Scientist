# A configured semantic screen with unreachable credentials is a failed control
# and must refuse; forced offline is a deliberate mode and returns the
# deterministic baseline.

from __future__ import annotations

import logging
from typing import Any

import pytest
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.domains.safety import gate as safety

from ._llm_fake_backend import completion_response, install_completion_backend

_MODEL = "openrouter/test-safety-model"
_OTHER_PROVIDER_KEY = "ANTHROPIC_API_KEY"
_MODEL_PROVIDER_KEY = "OPENROUTER_API_KEY"
_TEXT = "A benign research goal."
_DUAL_USE = "# Report\nThis programme is explicitly dual-use."


@pytest.fixture(autouse=True)
def _screen_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "semantic_safety_enabled", True)
    monkeypatch.setattr(settings, "semantic_safety_model", _MODEL)


def _env(monkeypatch: pytest.MonkeyPatch, *keys: str, offline: bool = False) -> None:
    if offline:
        monkeypatch.setenv("COSCIENTIST_TEST_DOUBLE", "deterministic")
    else:
        monkeypatch.delenv("COSCIENTIST_TEST_DOUBLE", raising=False)
    for key in keys:
        monkeypatch.setenv(key, "sk-test")


class _Provider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply: str | Exception = '{"category":"allowed","reason":"model"}'

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return completion_response(self.reply)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    fake = _Provider()
    install_completion_backend(monkeypatch, fake)
    return fake


@pytest.mark.parametrize(
    ("offline", "keys", "enabled", "expected", "calls"),
    [
        (True, (_MODEL_PROVIDER_KEY,), True, ("allow", "deterministic"), 0),
        (False, (), True, ("hold", ":no_credential"), 0),
        (False, (_OTHER_PROVIDER_KEY,), True, ("hold", ":no_credential"), 0),
        (False, (_OTHER_PROVIDER_KEY,), False, ("allow", "deterministic"), 0),
        (
            False,
            (_MODEL_PROVIDER_KEY,),
            True,
            ("allow", f"semantic:{_MODEL}"),
            1,
        ),
    ],
    ids=[
        "forced_offline",
        "keyless_production_refuses",
        "partial_deployment_refuses",
        "disabled_screen_is_configuration",
        "the_models_own_credential",
    ],
)
async def test_the_screen_runs_only_with_the_models_credential(
    monkeypatch: pytest.MonkeyPatch,
    provider: _Provider,
    caplog: pytest.LogCaptureFixture,
    offline: bool,
    keys: tuple[str, ...],
    enabled: bool,
    expected: tuple[str, str],
    calls: int,
) -> None:
    decision, assessor = expected
    _env(monkeypatch, *keys, offline=offline)
    monkeypatch.setattr(settings, "semantic_safety_enabled", enabled)

    with caplog.at_level(logging.WARNING, logger="co_scientist.domains.safety.semantic"):
        screened = await safety.screen_contextual(_TEXT, "intake")

    assert screened.decision == decision
    assert screened.assessor.endswith(assessor)
    assert len(provider.calls) == calls
    if decision == "hold":
        assert screened.requires_review is True
        assert screened.risk_domains == ["assessment_unavailable"]
        assert "credential" in caplog.text.lower()


async def test_a_scoped_byok_key_opens_the_screen_for_a_partial_deployment(
    monkeypatch: pytest.MonkeyPatch, provider: _Provider
) -> None:
    _env(monkeypatch, _OTHER_PROVIDER_KEY)
    byok = byok_scope.ByokCredential(provider="openrouter", api_key="sk-byok", model=_MODEL)

    with byok_scope.scoped_byok(byok):
        screened = await safety.screen_contextual(_TEXT, "intake")

    assert screened.assessor.startswith("semantic:")
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    ("keys", "calls"),
    [((_OTHER_PROVIDER_KEY,), 0), ((_MODEL_PROVIDER_KEY,), 1)],
)
async def test_the_model_cannot_downgrade_a_deterministic_redaction(
    monkeypatch: pytest.MonkeyPatch,
    provider: _Provider,
    keys: tuple[str, ...],
    calls: int,
) -> None:
    _env(monkeypatch, *keys)
    baseline = safety.screen_final(_DUAL_USE)
    assert baseline.decision == "redact"

    screened = await safety.screen_contextual(_DUAL_USE, "final", deterministic=baseline)

    assert screened.decision == "redact"
    assert len(provider.calls) == calls


@pytest.mark.parametrize(
    ("reply", "decision", "assessor"),
    [
        (
            '{"category":"uncertain","reason":"Ambiguous operational intent.",'
            '"risk_domains":["biology"]}',
            "hold",
            "semantic:",
        ),
        ('{"category":"prohibited","reason":"t"}', "block", "semantic:"),
        # json_object providers can wrap JSON in fences.
        (
            '```json\n{"category":"allowed","reason":"ok"}\n```',
            "allow",
            "semantic:",
        ),
        ("not json at all, and no fence to strip either", "hold", ":error"),
        (RuntimeError("provider unavailable"), "hold", ":error"),
    ],
    ids=[
        "uncertain",
        "prohibited",
        "markdown_fenced",
        "bad_reply",
        "provider_error",
    ],
)
async def test_the_models_verdict_can_raise_the_baseline_and_failure_holds(
    monkeypatch: pytest.MonkeyPatch,
    provider: _Provider,
    reply: str | Exception,
    decision: str,
    assessor: str,
) -> None:
    _env(monkeypatch, _MODEL_PROVIDER_KEY)
    provider.reply = reply

    screened = await safety.screen_contextual("Ambiguous protocol", "final")

    assert screened.decision == decision
    if assessor == "semantic:":
        assert screened.assessor.startswith(assessor)
    else:
        assert screened.assessor.endswith(assessor)
        assert screened.risk_domains == ["assessment_unavailable"]


@pytest.mark.parametrize(
    ("offline", "keys", "reply", "verdict"),
    [
        (False, (_MODEL_PROVIDER_KEY,), None, "allow"),
        (True, (_MODEL_PROVIDER_KEY,), None, None),
        (False, (_OTHER_PROVIDER_KEY,), None, None),
        (False, (_MODEL_PROVIDER_KEY,), RuntimeError("down"), None),
    ],
    ids=["credentialed", "offline_pinned", "no_credential", "provider_fails"],
)
async def test_a_hold_is_assessed_only_when_the_model_can_be_asked(
    monkeypatch: pytest.MonkeyPatch,
    provider: _Provider,
    offline: bool,
    keys: tuple[str, ...],
    reply: Exception | None,
    verdict: str | None,
) -> None:
    _env(monkeypatch, *keys, offline=offline)
    if reply:
        provider.reply = reply

    assessed = await safety.assess_hold_contextually("no-such-run", _TEXT, "hypothesis")

    assert (assessed.decision if assessed else None) == verdict
    assert bool(provider.calls) is (keys == (_MODEL_PROVIDER_KEY,) and not offline)
