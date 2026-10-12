from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.domains.access import credentials
from co_scientist.platform import db
from co_scientist.platform.db.models import RunRow

from tests._store_helpers import seed_run

_SECRET = "unit-test-byok-secret"
_KEY = "sk-test-1234567890"
_SECOND_KEY = "sk-second-0987654321"


@pytest.fixture
def byok_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "byok_encryption_key", _SECRET)
    return _SECRET


def _make_run(goal: str = "goal") -> RunRow:
    return seed_run(goal, profile="express")


def test_encrypt_decrypt_round_trip(byok_secret: str) -> None:
    token = credentials.encrypt_api_key(_KEY)
    assert token != _KEY
    assert credentials.decrypt_api_key(token) == _KEY


def test_idempotency_fingerprint_preserves_existing_receipts(byok_secret: str) -> None:
    assert credentials.idempotency_secret_fingerprint(_KEY) == (
        "c4bf2ac9cfdcd4bc1709a1a294fa04dd001a87a6e9e7766451ccaca5c203d60e"
    )


def test_idempotency_fingerprint_binds_both_secret_and_credential(
    byok_secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fingerprint = credentials.idempotency_secret_fingerprint(_KEY)
    assert fingerprint == credentials.idempotency_secret_fingerprint(_KEY)
    assert fingerprint != credentials.idempotency_secret_fingerprint(_SECOND_KEY)
    monkeypatch.setattr(settings, "byok_encryption_key", "other-deployment-secret")
    assert fingerprint != credentials.idempotency_secret_fingerprint(_KEY)


@pytest.mark.parametrize("fault", ["tampered", "other_secret"])
def test_decrypt_rejects_a_tampered_token_or_another_secret(
    byok_secret: str, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    token = credentials.encrypt_api_key(_KEY)
    if fault == "tampered":
        token = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    else:
        monkeypatch.setattr(settings, "byok_encryption_key", "another-secret")
    with pytest.raises(RuntimeError):
        credentials.decrypt_api_key(token)


@pytest.mark.parametrize(
    "use_secret",
    [credentials.encrypt_api_key, credentials.idempotency_secret_fingerprint],
)
def test_secret_dependent_calls_require_the_deployment_secret(
    use_secret: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "byok_encryption_key", "")
    with pytest.raises(credentials.ByokNotConfiguredError):
        use_secret(_KEY)


def test_run_credential_round_trips_encrypted(byok_secret: str) -> None:
    run = _make_run()
    cred = byok_scope.ByokCredential(
        provider="deepseek", api_key=_KEY, model="deepseek/deepseek-v4-flash"
    )
    credentials.store_run_credential(run.id, run.client_id, cred)
    assert credentials.get_run_credential(run.id) == cred
    with db.connect() as conn:
        row = conn.execute(
            "SELECT encrypted_key FROM run_credentials WHERE run_id=?",
            (run.id,),
        ).fetchone()
    assert _KEY not in row["encrypted_key"]


def test_run_credential_deleted_with_the_run(byok_secret: str) -> None:
    run = _make_run()
    cred = byok_scope.ByokCredential(provider="openai", api_key=_KEY, model="openai/gpt-4o")
    credentials.store_run_credential(run.id, run.client_id, cred)
    with db.connect() as conn:
        conn.execute("DELETE FROM runs WHERE id=?", (run.id,))
    assert credentials.get_run_credential(run.id) is None


def test_byok_model_and_key_prefers_scoped_credential(
    byok_secret: str,
) -> None:
    cred = byok_scope.ByokCredential(provider="openai", api_key=_KEY, model="openai/gpt-4o")
    assert byok_scope.byok_model_and_key("chat-model") == (
        "chat-model",
        None,
    )
    with byok_scope.scoped_byok(cred):
        assert byok_scope.byok_model_and_key("chat-model") == (
            "openai/gpt-4o",
            _KEY,
        )
    assert byok_scope.byok_model_and_key("chat-model") == (
        "chat-model",
        None,
    )


def _mixed() -> byok_scope.ByokCredential:
    return byok_scope.ByokCredential(
        provider="openai",
        api_key=_KEY,
        model="openai/gpt-6.1-sol",
        supervisor_model="gemini/gemini-3.8-flash",
        supervisor_provider="gemini",
        supervisor_api_key=_SECOND_KEY,
    )


def test_redaction_scrubs_both_keys(byok_secret: str) -> None:
    message = f"{_KEY} then {_SECOND_KEY}"
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, message, (), None)
    with byok_scope.scoped_byok(_mixed()):
        assert byok_scope.ByokRedactionFilter().filter(record)
    assert record.getMessage() == "[REDACTED] then [REDACTED]"


async def test_validation_error_never_echoes_the_supervisor_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acompletion(**kwargs: object) -> None:
        raise RuntimeError(f"exploded using {kwargs['api_key']}")

    monkeypatch.setattr(credentials, "_acompletion", fake_acompletion)
    with pytest.raises(credentials.ByokValidationError) as exc_info:
        await credentials.validate_byok_credential(_mixed())
    assert _KEY not in str(exc_info.value)
    assert _SECOND_KEY not in str(exc_info.value)
