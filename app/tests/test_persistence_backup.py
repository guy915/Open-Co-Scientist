from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from dev.backup_db import backup_database


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_deadline_never_publishes_a_backup(tmp_path: Path, timeout: float) -> None:
    source = tmp_path / "source.db"
    source.touch()
    with pytest.raises(ValueError, match="positive and finite"):
        backup_database(source, tmp_path / "backup.db", timeout)
    assert not list(tmp_path.glob("*backup*"))


def test_expired_copy_leaves_no_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (value TEXT)")
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr("dev.backup_db.time.monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        backup_database(source, tmp_path / "backup.db", timeout=1.0)
    assert not list(tmp_path.glob("*backup*"))


def test_backup_includes_committed_wal_and_has_private_permissions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('committed research')")
        connection.commit()
        assert Path(f"{source}-wal").stat().st_size > 0
        backup_database(source, destination)
        assert connection.execute("SELECT * FROM records").fetchall() == [("committed research",)]
    with sqlite3.connect(destination) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT * FROM records").fetchall() == [("committed research",)]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".backup.db.*"))


def test_missing_source_never_creates_an_empty_database(tmp_path: Path) -> None:
    source = tmp_path / "missing.db"
    destination = tmp_path / "backup.db"
    with pytest.raises(FileNotFoundError):
        backup_database(source, destination)
    assert not source.exists()
    assert not destination.exists()


def test_existing_destination_is_preserved(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.touch()
    destination = tmp_path / "backup.db"
    destination.write_bytes(b"keep the previous backup")
    with pytest.raises(FileExistsError):
        backup_database(source, destination)
    assert destination.read_bytes() == b"keep the previous backup"


def test_failed_backup_leaves_no_published_or_temporary_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "corrupt.db"
    source.write_bytes(b"not a SQLite database")
    destination = tmp_path / "backup.db"
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".backup.db.*"))
