from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from co_scientist.core.exceptions import LLMCallBudgetExceededError
from co_scientist.platform.db import transaction
from co_scientist.platform.llm import complete_request
from co_scientist.platform.llm.admission import call_budget
from co_scientist.platform.llm.admission.service import scoped_client
from tests._llm_fake import install_fake_backend


def test_eviction_cannot_erase_an_inflight_ceiling() -> None:
    with call_budget.scoped_llm_call_budget("victim", 1):
        call_budget.record_provider_request()
        for index in range(call_budget._MAX_TRACKED_RUNS + 1):
            with call_budget.scoped_llm_call_budget(f"other-{index}", 1):
                pass
        with pytest.raises(LLMCallBudgetExceededError):
            call_budget.record_provider_request()


def test_terminal_cleanup_cannot_refund_physical_attempts() -> None:
    with call_budget.scoped_llm_call_budget("completed", 1):
        call_budget.record_provider_request()
    call_budget.release_run_call_budget("completed")
    with (
        call_budget.scoped_llm_call_budget("completed", 1000),
        pytest.raises(LLMCallBudgetExceededError),
    ):
        call_budget.record_provider_request()


def test_process_restart_cannot_refund_physical_attempts() -> None:
    with call_budget.scoped_llm_call_budget("restart", 1):
        call_budget.record_provider_request()
    code = (
        "from co_scientist.platform.llm.admission.call_budget import "
        "scoped_llm_call_budget, record_provider_request; "
        "from co_scientist.core.exceptions import LLMCallBudgetExceededError\n"
        "try:\n"
        " with scoped_llm_call_budget('restart', 1): record_provider_request()\n"
        "except LLMCallBudgetExceededError: raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()


def test_none_cannot_downgrade_a_previously_bounded_run() -> None:
    with call_budget.scoped_llm_call_budget("downgrade", 1):
        call_budget.record_provider_request()
    call_budget.release_run_call_budget("downgrade")
    with (
        call_budget.scoped_llm_call_budget("downgrade", None),
        pytest.raises(LLMCallBudgetExceededError),
    ):
        call_budget.record_provider_request()


def test_concurrent_requests_share_the_durable_ceiling() -> None:
    def request(_: int) -> bool:
        with call_budget.scoped_llm_call_budget("race", 3):
            try:
                call_budget.record_provider_request()
            except LLMCallBudgetExceededError:
                return False
            return True

    with ThreadPoolExecutor(max_workers=12) as executor:
        assert sum(executor.map(request, range(24))) == 3
    assert call_budget.current_run_call_count("race") == 3


def test_explicit_client_store_survives_cleanup(tmp_path: Path) -> None:
    path = str(tmp_path / "explicit.db")
    with scoped_client("owner", db_path=path):
        with call_budget.scoped_llm_call_budget("explicit", 1):
            call_budget.record_provider_request()
        call_budget.release_run_call_budget("explicit")
        with (
            call_budget.scoped_llm_call_budget("explicit", 1),
            pytest.raises(LLMCallBudgetExceededError),
        ):
            call_budget.record_provider_request()


def test_missing_durable_state_fails_closed() -> None:
    with call_budget.scoped_llm_call_budget("missing", 1):
        with transaction() as conn:
            conn.execute("DELETE FROM run_call_admissions WHERE run_id='missing'")
        with pytest.raises(LLMCallBudgetExceededError):
            call_budget.record_provider_request()


async def test_dispatch_is_reserved_before_network_without_holding_the_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider(**kwargs: Any) -> dict[str, Any]:
        with transaction() as conn:
            row = conn.execute(
                "SELECT calls FROM run_call_admissions WHERE run_id='dispatch'"
            ).fetchone()
            assert row is not None and row["calls"] == 1
        raise ValueError("synthetic failed dispatch")

    fake = install_fake_backend(monkeypatch, provider)
    with (
        call_budget.scoped_llm_call_budget("dispatch", 1),
        pytest.raises(ValueError, match="synthetic failed dispatch"),
    ):
        await complete_request(
            {"model": "gpt-4o-mini", "max_tokens": 10},
            "gpt-4o-mini",
            byok=True,
            timeout_seconds=1,
        )
    call_budget.release_run_call_budget("dispatch")
    with (
        call_budget.scoped_llm_call_budget("dispatch", 1),
        pytest.raises(LLMCallBudgetExceededError),
    ):
        await complete_request(
            {"model": "gpt-4o-mini", "max_tokens": 10},
            "gpt-4o-mini",
            byok=True,
            timeout_seconds=1,
        )
    assert len(fake.requests) == 1
