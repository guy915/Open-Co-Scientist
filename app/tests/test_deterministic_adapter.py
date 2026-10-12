from __future__ import annotations

import pytest
from co_scientist.core.config import PROVIDER_CREDENTIAL_ENV
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.orchestration import engine_adapter
from co_scientist.platform.llm import process_mode


def test_missing_keys_and_retired_flag_never_enable_deterministic_product_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COSCIENTIST_TEST_DOUBLE", raising=False)
    monkeypatch.setenv("COSCIENTIST_FORCE_OFFLINE", "1")
    for names in PROVIDER_CREDENTIAL_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    assert not process_mode.offline_mode()
    assert process_mode.production_routing_enabled()
    assert engine_adapter.system_status()["llm_backend"] == "real"
    with pytest.raises(ProviderAdmissionError, match="No model is available right now"):
        engine_adapter.resolve_offline_backend({"llm_backend": "offline"})


def test_explicit_adapter_keeps_test_execution_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COSCIENTIST_TEST_DOUBLE", "deterministic")
    assert process_mode.offline_mode()
    assert not process_mode.production_routing_enabled()
    assert engine_adapter.resolve_offline_backend({"llm_backend": "offline"})
