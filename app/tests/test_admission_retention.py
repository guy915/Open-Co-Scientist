from __future__ import annotations

from co_scientist.platform.db import connect
from co_scientist.platform.db.admission import ProviderReservation, run_host, settle_provider
from co_scientist.platform.db.admission_retention import purge_expired_admission_history

from tests._client import create_run, make_client

_DAY = 86_400
_NOW = 100 * _DAY + 3600


def test_retention_preserves_current_quota_and_recent_late_settlement() -> None:
    with connect() as conn:
        for day in (80, 100):
            conn.execute("INSERT INTO provider_admissions VALUES (?,'global','',5,100)", (day,))
            conn.execute(
                "INSERT INTO provider_token_reservations VALUES (?,?,?, ?,100,0,NULL)",
                (f"receipt-{day}", day, "deleted-owner", "host"),
            )
    assert purge_expired_admission_history(now=_NOW) == 2
    with connect() as conn:
        assert tuple(conn.execute("SELECT calls,tokens FROM provider_admissions").fetchone()) == (
            5,
            100,
        )
        assert [row[0] for row in conn.execute("SELECT id FROM provider_token_reservations")] == [
            "receipt-100"
        ]
        path = str(conn.execute("PRAGMA database_list").fetchone()[2])
    settle_provider(ProviderReservation("receipt-100", path), 40)
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT used_tokens FROM provider_token_reservations WHERE id='receipt-100'"
            ).fetchone()[0]
            == 40
        )


def test_old_live_run_host_survives_but_orphan_and_continuation_history_expires() -> None:
    run = create_run(make_client(), "retained research", headers={"X-Client-ID": "owner"}).json()[
        "id"
    ]
    with connect() as conn:
        for identifier in (run, "deleted-old-run", f"continuation:{run}:1"):
            conn.execute(
                "INSERT OR REPLACE INTO run_admissions VALUES (?,'owner','host',80,1)",
                (identifier,),
            )
    assert purge_expired_admission_history(now=_NOW) == 2
    assert run_host(run) == "host"
    with connect() as conn:
        assert [row[0] for row in conn.execute("SELECT run_id FROM run_admissions")] == [run]


def test_every_expired_counter_family_is_swept_without_a_new_request() -> None:
    with connect() as conn:
        conn.execute("INSERT INTO anonymous_admissions VALUES (80,'host','owner')")
        conn.execute("INSERT INTO input_admissions VALUES (80,'client','owner',1,1)")
        conn.execute("INSERT INTO app_llm_usage VALUES (80,'owner',1,1)")
        conn.execute("INSERT INTO feedback_admissions VALUES ('owner','host',?)", (80 * _DAY,))
        conn.execute("INSERT INTO free_run_usage VALUES ('old','owner',?)", (80 * _DAY,))
        conn.execute("INSERT INTO log_ingest_admissions VALUES (?,1,1,1)", (80 * 1440,))
    assert purge_expired_admission_history(now=_NOW) == 6
    assert purge_expired_admission_history(now=_NOW) == 0


def test_history_sweeps_are_bounded_and_keep_seven_utc_days() -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO anonymous_admissions VALUES (80,'host',?)",
            [(f"owner-{i}",) for i in range(501)],
        )
        conn.execute("INSERT INTO anonymous_admissions VALUES (94,'host','boundary-owner')")
    assert purge_expired_admission_history(now=_NOW) == 500
    assert purge_expired_admission_history(now=_NOW) == 1
    with connect() as conn:
        assert (
            conn.execute("SELECT client_id FROM anonymous_admissions").fetchone()[0]
            == "boundary-owner"
        )
