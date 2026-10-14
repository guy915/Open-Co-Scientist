from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from co_scientist.core.config import settings
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import connect
from co_scientist.platform.db.admission_retention import purge_expired_admission_history
from co_scientist.platform.llm.admission.service import (
    reserve_physical,
    scoped_client,
    settle_physical,
)
from co_scientist.platform.llm.request.azure import LUNA
from co_scientist.platform.llm.request.backend import using_backend
from co_scientist.platform.llm.request.transport import complete_request

from tests._azure_ledger import record_azure_allowance


@pytest.fixture
def budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    path = str(tmp_path / "spend.db")
    monkeypatch.setenv("COSCIENTIST_DB_PATH", path)
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "1")
    monkeypatch.setenv("LLM_AZURE_ENABLED", "true")
    monkeypatch.setenv("LLM_AZURE_EXPIRES_AT", "2099-01-04T00:00:00+00:00")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_USD_TO_EUR", "0.88")
    record_azure_allowance(path)
    return path


def _request(model: str = LUNA) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": "answer"}], "max_tokens": 1000}


def _usage(**changes: Any) -> Any:
    return SimpleNamespace(usage={"prompt_tokens": 90, "completion_tokens": 30, **changes})


def _settleable(**changes: Any) -> Any:
    return _usage(prompt_tokens_details={"cache_write_tokens": 0}, **changes)


def _spent(path: str) -> int:
    with connect(path) as conn:
        return int(
            conn.execute("SELECT COALESCE(SUM(charged_microeur),0) FROM llm_spend").fetchone()[0]
        )


def test_concurrent_calls_never_reserve_past_total(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "0.0025")

    def reserve(_: int) -> bool:
        try:
            with scoped_client("owner", db_path=budget):
                reserve_physical(_request())
            return True
        except ProviderAdmissionError:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(reserve, range(40)))
    assert sum(results) == 2
    assert 0 < _spent(budget) <= 2500
    with connect(budget) as conn:
        assert (
            conn.execute("SELECT calls FROM provider_admissions WHERE scope='global'").fetchone()[0]
            == 2
        )


def test_token_and_money_settlement_is_one_idempotent_refund(budget: str) -> None:
    receipt = reserve_physical(_request())
    reserved = _spent(budget)
    settle_physical(
        receipt, _usage(prompt_tokens_details={"cached_tokens": 40, "cache_write_tokens": 0})
    )
    assert _spent(budget) == 18 and _spent(budget) < reserved
    settle_physical(receipt, _usage(prompt_tokens=1, completion_tokens=1))
    assert _spent(budget) == 18
    with connect(budget) as conn:
        assert all(row[0] == 120 for row in conn.execute("SELECT tokens FROM provider_admissions"))


def test_missing_usage_and_unknown_cache_write_keep_money_reservation(budget: str) -> None:
    missing = reserve_physical(_request())
    luna = reserve_physical(_request(LUNA))
    reserved = _spent(budget)
    settle_physical(missing, SimpleNamespace(usage=None))
    settle_physical(luna, _usage())
    assert _spent(budget) == reserved
    with connect(budget) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_spend WHERE settled=1").fetchone()[0] == 0


def test_settlement_keeps_original_fx_when_configuration_changes(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = reserve_physical(_request())
    monkeypatch.setenv("LLM_USD_TO_EUR", "100")
    settle_physical(receipt, _settleable())
    assert _spent(budget) == 22


def test_failed_settlement_rolls_back_token_refund_and_stops_new_paid_calls(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = reserve_physical(_request())
    before = _spent(budget)

    def fail(*_: Any) -> None:
        raise RuntimeError("storage failed")

    monkeypatch.setattr("co_scientist.platform.db.admission.settle_spend", fail)
    with pytest.raises(ProviderAdmissionError):
        settle_physical(receipt, _settleable())
    assert _spent(budget) == before
    with connect(budget) as conn:
        assert (
            conn.execute(
                "SELECT used_tokens FROM provider_token_reservations WHERE id=?", (receipt.id,)
            ).fetchone()[0]
            is None
        )
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())


def test_total_can_increase_within_the_recorded_allowance_without_resetting_history(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = reserve_physical(_request())
    amount = _spent(budget)
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", str(amount / 1_000_000))
    with pytest.raises(ProviderAdmissionError, match="No model is available"):
        reserve_physical(_request())
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "1")
    reserve_physical(_request())
    assert _spent(budget) == 2 * amount
    assert receipt.paid


def test_paid_calls_keep_per_client_token_and_call_limits(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "provider_client_calls_per_day", 1)
    reserve_physical(_request())
    with pytest.raises(ProviderAdmissionError, match="daily provider admission"):
        reserve_physical(_request())


def test_free_call_has_no_currency_charge(budget: str) -> None:
    receipt = reserve_physical(_request("test/free"))
    settle_physical(receipt, _usage())
    assert not receipt.paid and _spent(budget) == 0


def test_usage_outside_reserved_bound_keeps_charge_and_sets_durable_hold(budget: str) -> None:
    receipt = reserve_physical(_request())
    before = _spent(budget)
    with pytest.raises(ProviderAdmissionError):
        settle_physical(receipt, _settleable(prompt_tokens=1_000_000))
    assert _spent(budget) == before
    with connect(budget) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_spend_holds").fetchone()[0] == 1


def test_sub_microeur_total_never_rounds_up_into_extra_credit(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "0.0000009")
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request())
    assert _spent(budget) == 0


def test_money_total_does_not_loosen_free_run_limit(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.domains.access.free_usage import FreeUsageExhaustedError, claim_free_run
    from co_scientist.platform.db import transaction

    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "10000")
    with transaction(budget) as conn:
        for index in range(settings.free_runs_per_day):
            claim_free_run(conn, "owner", f"run-{index}")
    with pytest.raises(FreeUsageExhaustedError), transaction(budget) as conn:
        claim_free_run(conn, "owner", "one-extra")


@pytest.mark.parametrize("model_name", [LUNA, "azure/responses/gpt-6-luna-2026-09-22"])
async def test_kill_switch_rechecked_before_dispatch_keeps_reservation_without_http(
    budget: str,
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
) -> None:
    calls = []

    class Fake:
        async def complete(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return _usage()

        def supports_json_schema(self, model_name: str) -> bool:
            return True

    with using_backend(Fake()), pytest.raises(ProviderAdmissionError):
        await complete_request(
            _request(),
            model_name,
            byok=False,
            timeout_seconds=5,
            before_dispatch=lambda: monkeypatch.setenv("LLM_AZURE_ENABLED", "false"),
        )
    assert calls == [] and _spent(budget) > 0


@pytest.mark.parametrize(
    "name,value",
    [
        ("LLM_TOTAL_BUDGET_EUR", ""),
        ("LLM_TOTAL_BUDGET_EUR", "nan"),
        ("LLM_AZURE_EXPIRES_AT", "2020-01-04T00:00:00+00:00"),
        ("LLM_AZURE_ENABLED", "false"),
        ("LLM_ENABLED", "false"),
    ],
)
async def test_unset_spent_expired_and_disabled_policy_calls_no_provider(
    budget: str, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    calls = []

    class Fake:
        async def complete(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return _usage()

        def supports_json_schema(self, model_name: str) -> bool:
            return True

    monkeypatch.setenv(name, value)
    with (
        using_backend(Fake()),
        pytest.raises(ProviderAdmissionError, match="No model is available"),
    ):
        await complete_request(_request(), LUNA, byok=False, timeout_seconds=5)
    assert calls == [] and _spent(budget) == 0


def test_paid_reservation_is_synced_and_survives_abrupt_process_exit(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.db.spend import reserve_spend

    real_reserve = reserve_spend
    modes = []

    def observe(conn: Any, *args: Any) -> None:
        modes.append(conn.execute("PRAGMA synchronous").fetchone()[0])
        real_reserve(conn, *args)

    monkeypatch.setattr("co_scientist.platform.db.admission.reserve_spend", observe)
    reserve_physical(_request())
    assert modes == [2]
    first = _spent(budget)
    child_env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH") if key in os.environ}
    child_env.update(
        {
            "COSCIENTIST_DB_PATH": budget,
            "LLM_TOTAL_BUDGET_EUR": "1",
            "LLM_AZURE_ENABLED": "true",
            "LLM_AZURE_EXPIRES_AT": "2099-01-04T00:00:00+00:00",
            "LLM_ENABLED": "true",
            "LLM_USD_TO_EUR": "0.88",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "PYTHON_DOTENV_DISABLED": "1",
        }
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from co_scientist.platform.llm.admission.service import reserve_physical
reserve_physical({
    "model": "azure/gpt-6-luna-2026-09-22",
    "messages": [{"role": "user", "content": "answer"}],
    "max_tokens": 1000,
})
os._exit(0)
""",
        ],
        env=child_env,
        check=True,
        timeout=30,
    )
    assert _spent(budget) == 2 * first
    with connect(budget) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_spend WHERE settled=0").fetchone()[0] == 2


async def test_supported_byok_provider_keeps_caller_key_and_money_exemption(budget: str) -> None:
    captured = []

    class Fake:
        async def complete(self, **kwargs: Any) -> Any:
            captured.append(kwargs)
            return _usage()

        def supports_json_schema(self, model_name: str) -> bool:
            return True

    request = {**_request("openai/gpt-4o-mini"), "api_key": "caller-key"}
    with using_backend(Fake()):
        await complete_request(request, request["model"], byok=True, timeout_seconds=5)
    assert len(captured) == 1 and captured[0]["api_key"] == "caller-key"
    assert _spent(budget) == 0
    with connect(budget) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_admissions").fetchone()[0] == 0


def test_admission_retention_keeps_lifetime_spend(budget: str) -> None:
    with scoped_client("owner", db_path=budget):
        known = reserve_physical(_request())
        unknown = reserve_physical(_request())
    settle_physical(known, _settleable())
    charged = _spent(budget)
    with connect(budget) as conn:
        day = conn.execute("SELECT day FROM provider_token_reservations LIMIT 1").fetchone()[0]
    purge_expired_admission_history(now=(day + 8) * 86400)
    assert _spent(budget) == charged
    with connect(budget) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_token_reservations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_spend").fetchone()[0] == 2
        assert (
            conn.execute("SELECT settled FROM llm_spend WHERE id=?", (unknown.id,)).fetchone()[0]
            == 0
        )
    settle_physical(unknown, _settleable())
    assert _spent(budget) == 44


def test_cache_write_charge_is_durable_and_can_exhaust_remaining_total(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = reserve_physical(_request(LUNA))
    settle_physical(
        receipt,
        _usage(
            prompt_tokens=1000,
            completion_tokens=100,
            prompt_tokens_details={"cached_tokens": 800, "cache_write_tokens": 1000},
        ),
    )
    # A prompt below Luna's 272K boundary pays short rates; writes are an
    # additional charge.
    assert _spent(budget) == 179
    with connect(budget) as conn:
        row = conn.execute("SELECT cache_write_tokens, settled FROM llm_spend").fetchone()
        assert tuple(row) == (1000, 1)
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "0.000179")
    with pytest.raises(ProviderAdmissionError):
        reserve_physical(_request(LUNA))
