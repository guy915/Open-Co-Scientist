from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from co_scientist.api import spend_api
from co_scientist.core.config import settings
from co_scientist.platform.db import transaction
from co_scientist.platform.db.spend_view import spend_snapshot
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._azure_ledger import record_azure_allowance


def _paid(conn: Any, receipt: str, now: float, amount: int, *, settled: bool) -> None:
    conn.execute(
        "INSERT INTO llm_spend (id,created_at,model,role,reserved_microeur,charged_microeur,"
        "input_bound,output_bound,rates,settled,prompt_tokens,output_tokens,"
        "cached_tokens,cache_write_tokens) "
        "VALUES (?,?,?,?,?,?,1000,1000,'{}',?,100,10,40,20)",
        (receipt, now, "azure/model", "claims", amount, amount, int(settled)),
    )


def test_private_snapshot_counts_real_cache_cost_and_keeps_unknown_holds(
    isolated_db: str,
) -> None:
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc).timestamp()
    with transaction(isolated_db) as conn:
        _paid(conn, "today", now - 10, 2_000_000, settled=True)
        _paid(conn, "sunday", now - 4 * 86400, 1_000_000, settled=True)
        _paid(conn, "old", now - 8 * 86400, 4_000_000, settled=True)
        _paid(conn, "unknown", now, 3_000_000, settled=False)
        conn.execute(
            "INSERT INTO runs (id,research_goal,profile,status,provider,config_json,"
            "created_at,updated_at) VALUES ('run','public goal','express','queued',"
            "'engine','{}',?,?)",
            (now, now),
        )
        conn.execute("INSERT INTO llm_routes VALUES ('run','azure',1,3000000)")
        result = spend_snapshot(
            conn,
            now=now,
            total_microeur=20_000_000,
            credit_microusd=100_000_000,
            cycle_start=now,
            cycle_end=now + 86400,
        )
    azure = result["azure"]
    assert azure["today_eur"] == 2
    assert azure["week_eur"] == 2
    assert azure["total_spent_eur"] == 7
    assert azure["reserved_eur"] == 3
    assert azure["run_forecasts_eur"] == 3
    assert azure["remaining_eur"] == 7
    assert azure["burn_eur_per_day"] == pytest.approx(3 / 7)
    assert azure["days_at_current_rate"] == pytest.approx(7 / (3 / 7))
    assert result["cache_by_role"] == [
        {
            "currency": "EUR",
            "role": "claims",
            "calls": 3,
            "prompt_tokens": 300,
            "cache_read_tokens": 120,
            "cache_write_tokens": 60,
            "cost": 7,
            "refusal_measured_calls": 0,
            "refusals": 0,
        }
    ]


def test_view_requires_operator_token_and_stays_readable_when_kill_switch_is_off(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(spend_api.router)
    monkeypatch.setattr(settings, "logs_admin_token", "test-operator")
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("LLM_TOTAL_BUDGET_EUR", "175.99")
    record_azure_allowance()
    with TestClient(app) as client:
        assert client.get("/api/spend").status_code == 404
        assert client.get("/api/spend", headers={"X-Logs-Token": "wrong"}).status_code == 404
        reply = client.get("/api/spend", headers={"X-Logs-Token": "test-operator"})
    assert reply.status_code == 200
    assert reply.headers["cache-control"] == "no-store"
    assert reply.json()["azure"]["remaining_eur"] == 175.99
    assert not reply.json()["azure"]["available"]
    assert reply.json()["azure"]["days_at_current_rate"] is None
    assert reply.json()["anthropic"]["remaining_credit_usd"] == 100
    monkeypatch.delenv("LLM_TOTAL_BUDGET_EUR")
    with TestClient(app) as client:
        assert (
            client.get("/api/spend", headers={"X-Logs-Token": "test-operator"}).json()["azure"][
                "remaining_eur"
            ]
            is None
        )


def test_current_credit_keeps_unknown_calls_from_earlier_cycles(isolated_db: str) -> None:
    with transaction(isolated_db) as conn:
        for receipt, cycle, amount, settled in (
            ("current", 100, 2_000_000, 1),
            ("old-paid", 0, 10_000_000, 1),
            ("old-unknown", 0, 3_000_000, 0),
        ):
            conn.execute(
                "INSERT INTO anthropic_credit (id,cycle_start,created_at,role,"
                "reserved_microusd,charged_microusd,settled,input_bound,output_bound,rates,"
                "prompt_tokens,output_tokens,cached_tokens,cache_write_tokens) "
                "VALUES (?,?,100,'claims',?,?,?,1000,1000,'{}',100,10,40,20)",
                (receipt, cycle, amount, amount, settled),
            )
        result = spend_snapshot(
            conn,
            now=101,
            total_microeur=20_000_000,
            credit_microusd=100_000_000,
            cycle_start=100,
            cycle_end=200,
        )
    credit = result["anthropic"]
    assert credit["cycle_spent_usd"] == 2
    assert credit["reserved_usd"] == 3
    assert credit["remaining_credit_usd"] == 95
    assert credit["usable_allowance_usd"] == 90
    assert result["cache_by_role"][0]["cost"] == 12
    # Rows settled before the refused column existed stay unmeasured.
    assert result["cache_by_role"][0]["refusal_measured_calls"] == 0


def test_refusals_are_counted_per_role_beside_the_money(isolated_db: str) -> None:
    with transaction(isolated_db) as conn:
        for receipt, role, refused in (
            ("a", "safety", 1),
            ("b", "safety", 0),
            ("c", "goal_text", 0),
            ("d", "goal_text", None),
        ):
            conn.execute(
                "INSERT INTO anthropic_credit (id,cycle_start,created_at,role,"
                "reserved_microusd,charged_microusd,settled,input_bound,output_bound,rates,"
                "prompt_tokens,output_tokens,refused) "
                "VALUES (?,100,100,?,10,10,1,1000,1000,'{}',100,10,?)",
                (receipt, role, refused),
            )
        result = spend_snapshot(
            conn,
            now=101,
            total_microeur=None,
            credit_microusd=None,
            cycle_start=100,
            cycle_end=200,
        )
    counts = {
        row["role"]: (row["calls"], row["refusal_measured_calls"], row["refusals"])
        for row in result["cache_by_role"]
    }
    assert counts == {"goal_text": (2, 1, 0), "safety": (2, 2, 1)}


def test_available_azure_needs_both_deployments_and_no_durable_hold(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "logs_admin_token", "test-operator")
    for name, value in (
        ("LLM_ENABLED", "true"),
        ("LLM_AZURE_ENABLED", "true"),
        ("LLM_TOTAL_BUDGET_EUR", "1"),
        ("LLM_AZURE_EXPIRES_AT", "2099-01-04T00:00:00+00:00"),
        ("LLM_USD_TO_EUR", "0.88"),
        ("COSCIENTIST_REQUIRE_FREE_MODELS", "false"),
        ("AZURE_OPENAI_API_KEY", "synthetic"),
        ("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com"),
        ("AZURE_OPENAI_DEPLOYMENT", "supervisor"),
    ):
        monkeypatch.setenv(name, value)
    record_azure_allowance()
    app = FastAPI()
    app.include_router(spend_api.router)
    with TestClient(app) as client:
        headers = {"X-Logs-Token": "test-operator"}
        assert client.get("/api/spend", headers=headers).json()["azure"]["available"]
        monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT")
        assert not client.get("/api/spend", headers=headers).json()["azure"]["available"]
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "supervisor")
        with transaction(isolated_db) as conn:
            conn.execute("INSERT INTO llm_spend_holds VALUES ('unknown settlement')")
        assert not client.get("/api/spend", headers=headers).json()["azure"]["available"]
