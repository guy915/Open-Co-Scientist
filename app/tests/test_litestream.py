from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from co_scientist.platform import db

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "scripts/api-entrypoint.sh"
_R2 = {
    "LITESTREAM_R2_BUCKET": "test-bucket",
    "LITESTREAM_R2_ENDPOINT": "https://r2.invalid",
    "LITESTREAM_R2_ACCESS_KEY_ID": "test-key",
    "LITESTREAM_R2_SECRET_ACCESS_KEY": "test-secret",
}


def _start(tmp_path: Path, settings: dict[str, str]) -> tuple[int, list[str]]:
    calls = tmp_path / "calls"
    for name, body in {
        "python": (
            'if [ "${2:-}" = co_scientist.platform.db.backup_service ]; then\n'
            '  echo supervisor >> "$CALLS"\n'
            '  exec "$ENTRYPOINT" --serve\n'
            "fi\n"
            'if [ "${2:-}" = co_scientist.platform.db.spend ]; then\n'
            '  echo "hold:$3" >> "$CALLS"\n'
            "  exit 0\n"
            "fi\n"
            'echo "app:${COSCIENTIST_LITESTREAM_ACTIVE:-off}" >> "$CALLS"\n'
        ),
        "litestream": (
            'echo "$1" >> "$CALLS"\n'
            'if [ "$1" = restore ] && [ -n "${RESTORED:-}" ]; then : > "$RESTORED"; fi\n'
            'if [ "$1" = restore ]; then exit "${RESTORE_STATUS:-0}"; fi\n'
            'exec "$ENTRYPOINT" --serve\n'
        ),
    }.items():
        executable = tmp_path / name
        executable.write_text("#!/bin/sh\nset -eu\n" + body)
        executable.chmod(0o755)
    env = {
        "PATH": str(tmp_path),
        "CALLS": str(calls),
        "ENTRYPOINT": str(_ENTRYPOINT),
        **settings,
    }
    result = subprocess.run([str(_ENTRYPOINT)], env=env, capture_output=True, check=False)
    return result.returncode, calls.read_text().splitlines()


@pytest.mark.parametrize("missing", [None, *_R2])
def test_incomplete_backup_configuration_never_invokes_litestream(
    tmp_path: Path, missing: str | None
) -> None:
    config = dict(_R2) if missing else {}
    if missing:
        del config[missing]
    config["COSCIENTIST_LITESTREAM_ACTIVE"] = "1"
    assert _start(tmp_path, config) == (0, ["app:off"])


def test_replication_wraps_the_app_only_after_successful_restore(tmp_path: Path) -> None:
    assert _start(tmp_path, _R2) == (0, ["restore", "supervisor", "app:1"])


def test_replica_restore_holds_azure_spend_before_serving(tmp_path: Path) -> None:
    restored = str(tmp_path / "restored.db")
    settings = {**_R2, "COSCIENTIST_DB_PATH": restored, "RESTORED": restored}
    assert _start(tmp_path, settings) == (
        0,
        ["restore", f"hold:{restored}", "supervisor", "app:1"],
    )


def test_existing_database_is_not_held_by_a_skipped_restore(tmp_path: Path) -> None:
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"")
    settings = {**_R2, "COSCIENTIST_DB_PATH": str(existing), "RESTORED": str(existing)}
    assert _start(tmp_path, settings) == (0, ["restore", "supervisor", "app:1"])


def test_restore_failure_never_starts_an_empty_database(tmp_path: Path) -> None:
    assert _start(tmp_path, {**_R2, "RESTORE_STATUS": "17"}) == (17, ["restore"])


def test_litestream_owns_checkpoints_while_serving(
    monkeypatch: pytest.MonkeyPatch, isolated_db: str
) -> None:
    with db.connect(isolated_db) as conn:
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
    monkeypatch.setenv("COSCIENTIST_LITESTREAM_ACTIVE", "1")
    with db.connect(isolated_db) as conn:
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
        conn.execute("CREATE TABLE checkpoint_probe (value TEXT)")
        conn.execute("INSERT INTO checkpoint_probe VALUES ('retained')")
        wal = Path(isolated_db + "-wal")
        before = wal.stat().st_size
        db.checkpoint_wal(isolated_db)
        assert wal.stat().st_size == before > 0
        assert conn.execute("SELECT value FROM checkpoint_probe").fetchone()[0] == "retained"
