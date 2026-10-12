import pytest

from co_scientist.core.config import (
    PROVIDER_CREDENTIAL_ENV,
    any_provider_credential,
    has_provider_credential,
)


def test_azure_admission_requires_the_dispatch_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    for names in PROVIDER_CREDENTIAL_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_API_KEY", "synthetic-retired-alias")
    assert not has_provider_credential("azure/deployment")
    assert not any_provider_credential()
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "synthetic-canonical-key")
    assert has_provider_credential("azure/deployment")
    assert any_provider_credential()
