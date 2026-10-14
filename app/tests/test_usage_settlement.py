from __future__ import annotations

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from co_scientist.core.config import settings
from co_scientist.platform import db
from co_scientist.platform.db.admission import settle_provider
from co_scientist.platform.llm.admission.service import (
    reserve_physical,
    scoped_app_admission,
    scoped_client,
    settle_physical,
)
from co_scientist.platform.llm.request.transport import complete_request

from tests._llm_fake_backend import install_completion_backend


async def test_reported_usage_replaces_every_reserved_token_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def respond(**kwargs: Any) -> Any:
        with db.transaction() as conn:
            conn.execute("SELECT COUNT(*) FROM provider_admissions").fetchone()
        return SimpleNamespace(
            choices=[], usage=SimpleNamespace(prompt_tokens=120, completion_tokens=80)
        )

    install_completion_backend(monkeypatch, respond)
    with scoped_client("owner", host="peer"), scoped_app_admission():
        await complete_request(
            {"model": "gpt-4o-mini", "max_tokens": 1000},
            "gpt-4o-mini",
            byok=False,
            timeout_seconds=1,
        )
    with db.connect() as conn:
        provider = conn.execute("SELECT scope,calls,tokens FROM provider_admissions").fetchall()
        app = conn.execute("SELECT calls,tokens FROM app_llm_usage").fetchone()
    assert {row["scope"] for row in provider} == {"global", "client", "host"}
    assert [(row["calls"], row["tokens"]) for row in provider] == [(1, 200)] * 3
    assert (app["calls"], app["tokens"]) == (1, 200)


async def test_standard_sized_work_finishes_under_unchanged_client_token_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert settings.provider_client_tokens_per_day == 16_000_000

    async def respond(**kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[], usage=SimpleNamespace(prompt_tokens=7000, completion_tokens=3000)
        )

    fake = install_completion_backend(monkeypatch, respond)
    with scoped_client("standard-owner", host="standard-peer"):
        for _ in range(450):
            await complete_request(
                {
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "x" * 30000}],
                    "max_tokens": 12000,
                },
                "gpt-4o-mini",
                byok=False,
                timeout_seconds=1,
            )
    assert len(fake.requests) == 450
    with db.connect() as conn:
        row = conn.execute(
            "SELECT calls,tokens FROM provider_admissions WHERE scope='client'"
        ).fetchone()
    assert (row["calls"], row["tokens"]) == (450, 4_500_000)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": 5},
        {"prompt_tokens": -1, "completion_tokens": 5},
        {"prompt_tokens": True, "completion_tokens": 5},
    ],
)
async def test_unknown_usage_keeps_reservation_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    isolated_db: str,
    usage: Any,
) -> None:
    async def respond(**kwargs: Any) -> Any:
        return SimpleNamespace(choices=[], usage=usage)

    install_completion_backend(monkeypatch, respond)
    with scoped_client("owner"), scoped_app_admission():
        await complete_request(
            {"model": "gpt-4o-mini", "max_tokens": 1000},
            "gpt-4o-mini",
            byok=False,
            timeout_seconds=1,
        )
    db._initialized.discard(isolated_db)
    with db.connect() as conn:
        reserved = conn.execute(
            "SELECT tokens,used_tokens FROM provider_token_reservations"
        ).fetchone()
        assert reserved["used_tokens"] is None
        assert all(
            row[0] == reserved["tokens"]
            for row in conn.execute("SELECT tokens FROM provider_admissions")
        )
        assert conn.execute("SELECT tokens FROM app_llm_usage").fetchone()[0] == reserved["tokens"]


def test_concurrent_duplicate_settlement_refunds_once(monkeypatch: pytest.MonkeyPatch) -> None:
    with scoped_client("owner", host="peer"), scoped_app_admission():
        receipt = reserve_physical({"max_tokens": 1000})
        outstanding = reserve_physical({"max_tokens": 1000})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: settle_provider(receipt, 200), range(8)))
    with db.connect() as conn:
        reserve = conn.execute(
            "SELECT tokens FROM provider_token_reservations WHERE id=?", (outstanding.id,)
        ).fetchone()[0]
        assert all(
            row[0] == reserve + 200
            for row in conn.execute("SELECT tokens FROM provider_admissions")
        )
        assert conn.execute("SELECT tokens FROM app_llm_usage").fetchone()[0] == reserve + 200


def test_late_settlement_uses_original_database_and_day(
    monkeypatch: pytest.MonkeyPatch,
    isolated_db: str,
) -> None:
    monkeypatch.setattr("co_scientist.platform.db.admission.current_time", lambda: 86400.0)
    with scoped_client("owner", db_path=isolated_db):
        receipt = reserve_physical({"max_tokens": 1000})
    monkeypatch.setattr("co_scientist.platform.db.admission.current_time", lambda: 172800.0)
    with scoped_client("owner", db_path=isolated_db):
        next_day = reserve_physical({"max_tokens": 1000})
    monkeypatch.setenv("COSCIENTIST_DB_PATH", isolated_db + ".other")
    settle_physical(
        receipt, SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    )
    with db.connect(isolated_db) as conn:
        reserve = conn.execute(
            "SELECT tokens FROM provider_token_reservations WHERE id=?", (next_day.id,)
        ).fetchone()[0]
        assert all(
            row[0] == reserve for row in conn.execute("SELECT tokens FROM provider_admissions")
        )


@pytest.mark.parametrize("finish", [True, False])
async def test_stream_only_settles_after_successful_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    finish: bool,
) -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield SimpleNamespace(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=80))

    async def respond(**kwargs: Any) -> Any:
        return chunks()

    install_completion_backend(monkeypatch, respond)
    with scoped_client("owner", host="peer"), scoped_app_admission():
        stream = await complete_request(
            {"model": "gpt-4o-mini", "max_tokens": 1000, "stream": True},
            "gpt-4o-mini",
            byok=False,
            timeout_seconds=1,
        )
    await stream.__anext__()
    if finish:
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
    await stream.aclose()
    with db.connect() as conn:
        row = conn.execute("SELECT tokens,used_tokens FROM provider_token_reservations").fetchone()
        assert row["used_tokens"] == (200 if finish else None)
        expected = 200 if finish else row["tokens"]
        assert all(r[0] == expected for r in conn.execute("SELECT tokens FROM provider_admissions"))
        assert conn.execute("SELECT tokens FROM app_llm_usage").fetchone()[0] == expected


def test_usage_above_upper_reservation_never_raises_or_refunds_limit() -> None:
    with scoped_client("owner"):
        receipt = reserve_physical({"max_tokens": 1000})
    settle_physical(
        receipt,
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100000, completion_tokens=100000)),
    )
    with db.connect() as conn:
        row = conn.execute("SELECT tokens,used_tokens FROM provider_token_reservations").fetchone()
        assert row["tokens"] == row["used_tokens"]


def test_settlement_failure_rolls_back_every_row() -> None:
    with scoped_client("owner", host="peer"), scoped_app_admission():
        receipt = reserve_physical({"max_tokens": 1000})
    with db.connect() as conn:
        reserve = conn.execute("SELECT tokens FROM provider_token_reservations").fetchone()[0]
        conn.execute(
            "CREATE TRIGGER reject_host_settlement BEFORE UPDATE ON provider_admissions "
            "WHEN OLD.scope='host' BEGIN SELECT RAISE(ABORT,'settlement failure'); END"
        )
    with pytest.raises(db.Error):
        settle_provider(receipt, 200)
    with db.connect() as conn:
        assert all(
            row[0] == reserve for row in conn.execute("SELECT tokens FROM provider_admissions")
        )
        assert conn.execute("SELECT tokens FROM app_llm_usage").fetchone()[0] == reserve
        assert (
            conn.execute("SELECT used_tokens FROM provider_token_reservations").fetchone()[0]
            is None
        )
