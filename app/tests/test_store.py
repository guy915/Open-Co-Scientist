from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from co_scientist.domains.research_state.repository import hypotheses as store
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.hypotheses import NewHypothesis
from co_scientist.domains.research_state.repository.records import (
    NewCitation,
    NewEvidence,
    NewReview,
)
from co_scientist.orchestration.repository import events as store_events
from co_scientist.platform import db as store_db
from co_scientist.platform.retrieval.citations import CitationState

from tests._store_helpers import seed_run


@pytest.fixture
def db(isolated_db: str) -> str:
    return os.environ["COSCIENTIST_DB_PATH"]


def test_event_log_is_append_only_and_strictly_increasing(db: str) -> None:
    run = seed_run("test", provider="mock")
    seqs = []
    for i in range(5):
        seqs.append(store_events.append_event(run.id, "log", {"i": i}))
    events = store_events.list_events(run.id)
    assert seqs == [e["seq"] for e in events]
    assert all(seqs[i] < seqs[i + 1] for i in range(4))


def test_multi_parent_hypothesis_records_every_parent(db: str) -> None:
    run = seed_run("multi-parent", provider="mock")
    primary = store.add_hypothesis(
        NewHypothesis(run_id=run.id, title="P1", statement="p1", hypothesis_id="p1")
    )
    partner = store.add_hypothesis(
        NewHypothesis(run_id=run.id, title="P2", statement="p2", hypothesis_id="p2")
    )
    child = store.add_hypothesis(
        NewHypothesis(
            run_id=run.id,
            title="C",
            statement="cs",
            hypothesis_id="c1",
            parent_id=primary,
            parent_ids=[primary, partner],
            generation=1,
            created_by_agent="evolution",
        )
    )

    rows = {r["id"]: r for r in store.list_hypotheses(run.id)}
    assert rows[child]["parent_id"] == primary
    assert rows[child]["parent_ids"] == [primary, partner]
    assert rows[child]["generation"] == 1
    assert rows[primary]["generation"] == 0
    assert rows[primary]["parent_id"] is None
    assert rows[primary]["parent_ids"] is None
    assert rows[partner]["parent_ids"] is None
    assert rows[child]["parent_ids"] == [primary, partner]


def test_redact_hypothesis_fields_overwrites_detail_columns(db: str) -> None:
    run = seed_run("redact", provider="mock")
    hid = store.add_hypothesis(
        NewHypothesis(
            run_id=run.id,
            title="H",
            statement="keep me",
            mechanism="secret mechanism",
            expected_effect="secret effect",
            experimental_context="secret protocol",
        )
    )
    store.redact_hypothesis_fields(
        hid,
        {"mechanism": "[X]", "expected_effect": "[X]", "experimental_context": "[X]"},
    )
    row = {r["id"]: r for r in store.list_hypotheses(run.id)}.get(hid)
    assert row is not None
    assert row["mechanism"] == "[X]"
    assert row["expected_effect"] == "[X]"
    assert row["experimental_context"] == "[X]"
    assert row["statement"] == "keep me"


def test_connections_pair_wal_with_normal_synchronous(db: str) -> None:
    # WAL with NORMAL reduces fsync lock time while checkpoints preserve
    # durability; writes remain single-writer.
    with store_db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


class _StaleColumnView:
    """A connection whose column listing predates another process's ALTER."""

    def __init__(self, conn: sqlite3.Connection, alter_error: str) -> None:
        self._conn = conn
        self._alter_error = alter_error

    def execute(self, sql: str, *args: Any) -> Any:
        if sql.startswith("PRAGMA table_info"):
            return []
        if sql.startswith("ALTER TABLE"):
            raise sqlite3.OperationalError(self._alter_error)
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _reopen_with_stale_columns(monkeypatch: pytest.MonkeyPatch, db: str, alter_error: str) -> None:
    with store_db.connect():
        pass
    store_db._initialized.discard(db)
    real_open = store_db._open_raw_connection
    monkeypatch.setattr(
        store_db,
        "_open_raw_connection",
        lambda path: cast(sqlite3.Connection, _StaleColumnView(real_open(path), alter_error)),
    )


def test_startup_tolerates_a_racing_process_adding_the_column(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reopen_with_stale_columns(monkeypatch, db, "duplicate column name: x")
    with store_db.connect():
        pass
    assert db in store_db._initialized


def test_startup_surfaces_other_migration_failures(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reopen_with_stale_columns(monkeypatch, db, "disk I/O error")
    with (
        pytest.raises(sqlite3.OperationalError, match="disk I/O error"),
        store_db.connect(),
    ):
        pass
    assert db not in store_db._initialized


def test_startup_removes_retired_deletion_guards_and_preserves_saved_work(db: str) -> None:
    with store_db.connect() as conn:
        conn.execute("CREATE TABLE saved_work (content TEXT)")
        conn.execute("INSERT INTO saved_work VALUES ('saved research')")
        for table in ("privacy_erased_clients", "privacy_erased_runs"):
            conn.execute(f"CREATE TABLE {table} (digest TEXT PRIMARY KEY, expires_at REAL)")
        conn.execute(
            "CREATE TRIGGER privacy_saved_work_insert BEFORE INSERT ON saved_work "
            "BEGIN SELECT cosci_owner_digest(NEW.content); END"
        )
    store_db._initialized.discard(db)

    with store_db.connect() as conn:
        conn.execute("INSERT INTO saved_work VALUES ('new research')")
        assert [row[0] for row in conn.execute("SELECT content FROM saved_work")] == [
            "saved research",
            "new research",
        ]
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name GLOB 'privacy_*'"
        ).fetchall()


def test_wal_checkpoint_failure_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="co_scientist.platform.db"):
        store_db.checkpoint_wal(str(tmp_path))
    assert "WAL checkpoint failed" in caplog.text


def _seed_cascade_children(run_id: str) -> None:
    hid = store.add_hypothesis(
        NewHypothesis(
            run_id=run_id,
            title="t",
            statement="s",
            created_by_agent="generation",
        )
    )
    eid = records.add_evidence(NewEvidence(run_id=run_id, title="paper", source="pubmed"))
    records.add_review(
        NewReview(
            run_id=run_id,
            hypothesis_id=hid,
            reviewer_agent="reflection",
            summary="sum",
            critique="crit",
        )
    )
    records.add_citation(
        NewCitation(
            run_id=run_id,
            hypothesis_id=hid,
            evidence_id=eid,
            claim="c",
            state=CitationState.VERIFIED,
        )
    )
    store_events.append_event(run_id, "log", {"i": 0})


def test_deleting_a_run_cascades_to_child_rows(db: str) -> None:
    run = seed_run("cascade", provider="mock")
    _seed_cascade_children(run.id)

    with store_db.connect() as conn:
        conn.execute("DELETE FROM runs WHERE id=?", (run.id,))

    assert store.list_hypotheses(run.id) == []
    assert records.list_evidence(run.id) == []
    assert records.list_reviews(run.id) == []
    assert records.list_citations(run.id) == []
    assert store_events.list_events(run.id) == []


_PER_RUN_LISTED_TABLES = (
    "evidence",
    "citations",
    "claim_evidence",
    "safety_decisions",
    "reviews",
    "matches",
    "proximity_edges",
)


@pytest.mark.parametrize("table", _PER_RUN_LISTED_TABLES)
def test_per_run_listing_never_scans_the_whole_table(db: str, table: str) -> None:
    # Run-scoped indexes avoid scanning other runs.
    with store_db.connect() as conn:
        plan = conn.execute(
            f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE run_id=? ORDER BY created_at ASC",
            ("any-run",),
        ).fetchall()
    detail = " ".join(row["detail"] for row in plan)
    assert f"SCAN {table}" not in detail, detail
