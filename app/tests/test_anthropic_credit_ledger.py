from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db.anthropic_credit import (
    CreditReservation,
    credit_record,
    disable_credit,
    reserve_credit,
    settle_credit_amount,
)
from co_scientist.platform.llm.admission.anthropic import (
    credit_rates,
    credit_settlement,
    cycle_bounds,
    usage_cost,
)


def test_provider_limit_refusal_rolls_credit_reservation_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from co_scientist.core.config import settings
    from co_scientist.platform.db import connect
    from co_scientist.platform.llm.admission.service import reserve_physical, scoped_client

    path = str(tmp_path / "provider.db")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_MONTHLY_CREDIT_USD", "100")
    monkeypatch.setattr(settings, "provider_client_calls_per_day", 1)
    request = {
        "model": "anthropic/claude-haiku-5-5",
        "messages": [{"role": "user", "content": "goal"}],
        "max_tokens": 1000,
    }
    with scoped_client("owner", db_path=path):
        reserve_physical(request)
        with pytest.raises(ProviderAdmissionError):
            reserve_physical(request)
    with connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM anthropic_credit").fetchone()[0] == 1
        assert (
            conn.execute("SELECT calls FROM provider_admissions WHERE scope='client'").fetchone()[0]
            == 1
        )


def _time(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def settle_credit(
    conn: sqlite3.Connection,
    receipt: str,
    *,
    prompt: int | None,
    output: int | None,
    cached: int | None,
    written: int | None,
) -> None:
    row = credit_record(conn, receipt)
    assert row is not None
    values = credit_settlement(row, prompt=prompt, output=output, cached=cached, written=written)
    if values is not None:
        charged, prompt, output, cached, written = values
        settle_credit_amount(
            conn,
            receipt,
            charged=charged,
            prompt=prompt,
            output=output,
            cached=cached,
            written=written,
        )


def test_price_change_does_not_reprice_an_admitted_call(
    credit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.platform.llm.admission import anthropic

    with sqlite3.connect(credit_path) as conn:
        reserve_credit(conn, "call", _quote(5_000), _time("2026-10-08"))

    def unexpected(*args: object) -> object:
        raise AssertionError("settlement must use saved prices")

    monkeypatch.setattr(anthropic, "model_profile", unexpected)
    with sqlite3.connect(credit_path) as conn:
        settle_credit(conn, "call", prompt=2000, output=500, cached=900, written=1000)
        assert conn.execute("SELECT charged_microusd FROM anthropic_credit").fetchone()[0] == 394


@pytest.fixture
def credit_path(tmp_path: Path) -> Path:
    path = tmp_path / "credit.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE anthropic_credit_cycles (start REAL PRIMARY KEY, end REAL NOT NULL);"
            "CREATE TABLE anthropic_credit_state (slot TEXT PRIMARY KEY, disabled_until REAL);"
            "CREATE TABLE anthropic_credit (id TEXT PRIMARY KEY, cycle_start REAL NOT NULL,"
            "created_at REAL NOT NULL, role TEXT NOT NULL, reserved_microusd INTEGER NOT NULL,"
            "charged_microusd INTEGER NOT NULL, settled INTEGER NOT NULL DEFAULT 0,"
            "input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL,"
            "prompt_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,"
            "cache_write_tokens INTEGER, rates TEXT NOT NULL, refused INTEGER);"
        )
    return path


def _quote(amount: int) -> CreditReservation:
    return CreditReservation(
        cycle_start=_time("2026-10-07"),
        cycle_end=_time("2026-11-07"),
        limit=95_000_000,
        amount=amount,
        role="claims",
        input_bound=100_000,
        output_bound=16_384,
        rates=credit_rates(),
    )


def test_concurrent_calls_cannot_cross_95_percent(credit_path: Path) -> None:
    def reserve(index: int) -> bool:
        with sqlite3.connect(credit_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                reserve_credit(conn, str(index), _quote(47_500_000), _time("2026-10-08"))
            except ProviderAdmissionError:
                return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(reserve, range(8)))
    assert sum(admitted) == 2
    with sqlite3.connect(credit_path) as conn:
        assert conn.execute("SELECT SUM(charged_microusd) FROM anthropic_credit").fetchone()[0] == (
            95_000_000
        )


def test_real_cache_usage_settles_once_without_double_counting(credit_path: Path) -> None:
    with sqlite3.connect(credit_path) as conn:
        reserve_credit(conn, "call", _quote(5_000), _time("2026-10-08"))
        settle_credit(conn, "call", prompt=2000, output=500, cached=900, written=1000)
        settle_credit(conn, "call", prompt=0, output=0, cached=0, written=0)
        row = conn.execute("SELECT charged_microusd, settled FROM anthropic_credit").fetchone()
    assert row == (394, 1)


def test_refusal_retains_billed_usage_and_missing_usage_keeps_reservation(
    credit_path: Path,
) -> None:
    with sqlite3.connect(credit_path) as conn:
        reserve_credit(conn, "refused", _quote(5_000), _time("2026-10-08"))
        settle_credit(conn, "refused", prompt=2000, output=0, cached=0, written=0)
        reserve_credit(conn, "unknown", _quote(5_000), _time("2026-10-08"))
        settle_credit(conn, "unknown", prompt=None, output=50, cached=0, written=0)
    with sqlite3.connect(credit_path) as conn:
        assert conn.execute(
            "SELECT id, charged_microusd, settled FROM anthropic_credit ORDER BY id"
        ).fetchall() == [("refused", 200, 1), ("unknown", 5000, 0)]


def test_billing_cooldown_survives_restart_until_reset(credit_path: Path) -> None:
    with sqlite3.connect(credit_path) as conn:
        disable_credit(conn, _time("2026-11-07"))
    with sqlite3.connect(credit_path) as conn, pytest.raises(ProviderAdmissionError):
        reserve_credit(conn, "blocked", _quote(1), _time("2026-11-06T23:59:59"))
    next_cycle = CreditReservation(
        **{
            **_quote(1).__dict__,
            "cycle_start": _time("2026-11-07"),
            "cycle_end": _time("2026-12-07"),
        }
    )
    with sqlite3.connect(credit_path) as conn:
        reserve_credit(conn, "reset", next_cycle, _time("2026-11-07"))


def test_changing_reset_day_cannot_create_an_overlapping_allowance(credit_path: Path) -> None:
    with sqlite3.connect(credit_path) as conn:
        reserve_credit(conn, "old", _quote(95_000_000), _time("2026-10-08"))
        shifted = CreditReservation(
            **{
                **_quote(1).__dict__,
                "cycle_start": _time("2026-10-08"),
                "cycle_end": _time("2026-11-08"),
            }
        )
        with pytest.raises(ProviderAdmissionError):
            reserve_credit(conn, "shifted", shifted, _time("2026-10-09"))


def test_old_unknown_call_still_holds_capacity_after_reset(credit_path: Path) -> None:
    with sqlite3.connect(credit_path) as conn:
        reserve_credit(conn, "unknown", _quote(95_000_000), _time("2026-10-08"))
        next_cycle = CreditReservation(
            **{
                **_quote(1).__dict__,
                "cycle_start": _time("2026-11-07"),
                "cycle_end": _time("2026-12-07"),
            }
        )
        with pytest.raises(ProviderAdmissionError):
            reserve_credit(conn, "next", next_cycle, _time("2026-11-07"))


@pytest.mark.parametrize(
    "now,day,start,end",
    [
        ("2026-10-08", 7, "2026-10-07", "2026-11-07"),
        ("2026-11-07", 7, "2026-11-07", "2026-12-07"),
        ("2027-01-01", 7, "2026-12-07", "2027-01-07"),
        ("2028-02-29", 31, "2028-02-29", "2028-03-31"),
    ],
)
def test_billing_cycles_use_utc_and_calendar_months(
    now: str, day: int, start: str, end: str
) -> None:
    assert cycle_bounds(_time(now), day) == (_time(start), _time(end))


def test_long_price_boundary_and_thinking_are_charged_from_own_usage() -> None:
    assert usage_cost(100_000, 1000, cached=0, written=0) == 10_500
    assert usage_cost(100_001, 1000, cached=0, written=0) == 52_501
