from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path

import pytest
from co_scientist.platform import db
from co_scientist.platform.db import backup_service as backups
from co_scientist.platform.db.launch_control import read_control, write_control

from tests._client import make_client, make_operator_client


@pytest.fixture
def fixture_replica(tmp_path: Path) -> tuple[str, str, Path, dict[str, str]]:
    database = tmp_path / "source.db"
    replica = tmp_path / "replica"
    replica.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('synthetic-only')")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"database": str(database), "replica": str(replica)}))
    binary = tmp_path / "litestream"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import datetime,json,os,signal,sqlite3,sys,time
from pathlib import Path
a=sys.argv[1:]
c=json.loads(Path(a[a.index('-config')+1]).read_text())
source=Path(c['database']); replica=Path(c['replica'])
active=replica/'daemon-active'
if a[0]=='replicate' and '-once' not in a:
    active.write_text(str(os.getpid()))
    def stop(sig,frame): raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop)
    try:
        while True: time.sleep(.01)
    finally: active.unlink(missing_ok=True)
elif a[0]=='replicate':
    assert '-force-snapshot' in a, 'idle backups require force'
    assert not active.exists(), 'two replication processes overlapped'
    with sqlite3.connect(source) as s, sqlite3.connect(replica/'base.db') as d:
        s.backup(d)
    (replica/'stamp').write_text(datetime.datetime.now(datetime.timezone.utc).isoformat())
elif a[0]=='restore':
    if (replica/'command-failure').exists():
        print('secret-must-stay-private',file=sys.stderr);sys.exit(17)
    if '-dry-run' in a:
        level=0 if (replica/'no-snapshot').exists() else 9
        print(json.dumps({'max_txid':'0000000000000001','files':[{'level':level,'timestamp':(replica/'stamp').read_text()}]}))
    else:
        target=Path(a[a.index('-o')+1])
        target.write_bytes((replica/'base.db').read_bytes())
        if (replica/'corrupt').exists(): target.write_bytes(b'corrupt')
else: raise AssertionError(a)
"""
    )
    binary.chmod(0o755)
    environment = {"PATH": os.defpath, "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    return str(binary), str(config), database, environment


def test_idle_database_refreshes_its_complete_base_and_verifies_recovery(
    fixture_replica: tuple[str, str, Path, dict[str, str]],
) -> None:
    binary, config, database, environment = fixture_replica
    before = database.read_bytes()
    first = backups.refresh(binary, config, database, environment=environment)
    replica = database.parent / "replica"
    (replica / "stamp").write_text("2020-01-01T00:00:00+00:00")
    second = backups.refresh(binary, config, database, environment=environment)
    assert first["txid"] == second["txid"]
    assert second["snapshot_at"] > time.time() - 10
    assert second["status"] == "verified"
    assert database.read_bytes() == before
    assert not list(database.parent.glob(".backup-verify-*"))


@pytest.mark.parametrize("failure", ["no-snapshot", "corrupt", "command-failure"])
def test_failed_snapshot_or_restore_never_reports_success_and_cleans_private_copy(
    fixture_replica: tuple[str, str, Path, dict[str, str]],
    failure: str,
) -> None:
    binary, config, database, environment = fixture_replica
    (database.parent / "replica" / failure).touch()
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)) as error:
        backups.refresh(binary, config, database, environment=environment)
    assert "secret-must-stay-private" not in str(error.value)
    assert not list(database.parent.glob(".backup-verify-*"))


def test_stale_restore_base_is_not_verified_as_a_new_snapshot(
    fixture_replica: tuple[str, str, Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary, config, database, environment = fixture_replica
    actual = backups.command

    def skip_upload(arguments: list[str], *, environment: dict[str, str] | None = None) -> str:
        if arguments[1] == "replicate":
            return ""
        return actual(arguments, environment=environment)

    backups.refresh(binary, config, database, environment=environment)
    (database.parent / "replica" / "stamp").write_text("2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(backups, "command", skip_upload)
    with pytest.raises(RuntimeError, match="not refreshed"):
        backups.refresh(binary, config, database, environment=environment)


def test_backup_failure_pauses_once_and_preserves_an_existing_operator_decision(
    isolated_db: str,
) -> None:
    database = Path(isolated_db)
    backups.pause_for_failure(database)
    control = read_control(db_path=isolated_db)
    assert control.paused and not control.drain and control.resumes_at is None
    backups.pause_for_failure(database)
    assert read_control(db_path=isolated_db).revision == control.revision
    with db.transaction(durable=True) as connection:
        write_control(
            connection,
            paused=True,
            drain=True,
            message="Owner decision",
            resumes_at=None,
            expected_revision=control.revision,
        )
    backups.pause_for_failure(database)
    assert read_control(db_path=isolated_db).message == "Owner decision"
    assert read_control(db_path=isolated_db).drain


def test_verified_status_is_private_atomic_metadata_not_database_content(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    status = {"status": "verified", "verified_at": time.time(), "txid": "0000000000000001"}
    backups.record_status(database, status)
    assert json.loads(backups.status_path(database).read_text()) == status
    assert backups.read_status(database) == status
    assert stat.S_IMODE(backups.status_path(database).stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".backup-status-*"))


def test_private_status_reader_is_bounded_and_drops_unrecognized_payload(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    backups.record_status(
        database,
        {
            "status": "failed",
            "verified_at": 100,
            "secret": "hidden",
            "seconds": float("nan"),
            "txid": "not-a-txid",
        },
    )
    assert backups.read_status(database) == {"status": "failed", "verified_at": 100}
    backups.status_path(database).write_text("x" * 4097)
    assert backups.read_status(database) is None


def test_backup_metadata_requires_operator_authority_and_is_absent_from_public_status(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COSCIENTIST_LITESTREAM_ACTIVE", "1")
    backups.record_status(
        Path(isolated_db),
        {"status": "failed", "verified_at": 100, "private": "synthetic-private-payload"},
    )
    visitor = make_client()
    assert visitor.get("/api/launch-control").status_code == 403
    operator = make_operator_client()
    response = operator.get("/api/launch-control")
    assert response.status_code == 200
    assert response.json()["backup"] == {
        "enabled": True,
        "verification": {"status": "failed", "verified_at": 100},
    }
    assert "synthetic-private-payload" not in response.text
    public = visitor.get("/api/launch-status")
    assert public.status_code == 200 and "backup" not in public.json()
    monkeypatch.delenv("COSCIENTIST_LITESTREAM_ACTIVE")
    assert operator.get("/api/launch-control").json()["backup"] == {
        "enabled": False,
        "verification": None,
    }


@pytest.mark.parametrize("payload", [{"status": []}, {"status": "failed", "seconds": 10**500}])
def test_corrupt_status_cannot_break_operator_reads(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    database = tmp_path / "source.db"
    backups.record_status(database, payload)
    assert backups.read_status(database) in (None, {"status": "failed"})


@pytest.mark.parametrize("api_startup_delay", [0.0, 0.5])
@pytest.mark.parametrize("snapshot_delay", [0.0, 2.0])
def test_api_keeps_serving_while_only_the_replication_daemon_is_replaced(
    fixture_replica: tuple[str, str, Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    api_startup_delay: float,
    snapshot_delay: float,
) -> None:
    binary, config, database, environment = fixture_replica
    # The fake daemon/forced snapshot asserts no overlap. A real independent
    # child stays alive through two real snapshots, whose subprocess startup
    # can outlast the child's otherwise fixed amount of serving work.
    progress = database.parent / "api-progress"
    completed = database.parent / "refresh-completed"
    api = database.parent / "api.py"
    api.write_text(
        "import time\nfrom pathlib import Path\n"
        f"time.sleep({api_startup_delay})\n"
        f"p=Path({str(progress)!r})\n"
        f"completed=Path({str(completed)!r})\n"
        "q=p.with_suffix('.next')\n"
        "def wait_for_refresh(count):\n"
        f" deadline=time.monotonic()+{backups.COMMAND_TIMEOUT_SECONDS}\n"
        " while not completed.exists() or int(completed.read_text())<count:\n"
        "  assert time.monotonic()<deadline, 'Snapshot acknowledgement missing'\n"
        "  time.sleep(.01)\n"
        "for n in range(150):\n"
        " if n==149: wait_for_refresh(1)\n"
        " q.write_text(str(n)); q.replace(p)\n time.sleep(.01)\n"
        "wait_for_refresh(2)\n"
    )
    monkeypatch.setattr(backups, "INTERVAL_SECONDS", 0.01)
    observed: list[int] = []
    actual_refresh = backups.refresh

    class SupervisorClock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def time(self) -> float:
            return time.time()

        def sleep(self, seconds: float) -> None:
            if len(observed) < 2:
                self.now += seconds
            time.sleep(0.01)

    monkeypatch.setattr(backups, "time", SupervisorClock())

    def refresh(binary: str, configuration: str, database: Path) -> dict[str, object]:
        deadline = time.monotonic() + 1.5
        while not progress.exists() or (observed and int(progress.read_text()) <= observed[-1]):
            assert time.monotonic() < deadline, "API child did not make progress"
            time.sleep(0.01)
        if not observed:
            time.sleep(snapshot_delay)
        result = actual_refresh(binary, configuration, database, environment=environment)
        observed.append(int(progress.read_text()))
        acknowledgement = completed.with_suffix(".next")
        acknowledgement.write_text(str(len(observed)))
        acknowledgement.replace(completed)
        return result

    monkeypatch.setattr(backups, "refresh", refresh)
    assert backups.serve(binary, config, database, [sys.executable, str(api)]) == 0
    assert len(observed) == 2 and observed[1] > observed[0]
    assert int(progress.read_text()) == 149
    assert not (database.parent / "replica" / "daemon-active").exists()


def test_daemon_exit_stops_the_api_instead_of_serving_without_replication(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "api-pid"
    command = [
        sys.executable,
        "-c",
        "import os,time; from pathlib import Path; "
        f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)",
    ]
    binary = tmp_path / "fails"
    binary.write_text(f"#!{sys.executable}\nraise SystemExit(17)\n")
    binary.chmod(0o755)
    assert backups.serve(str(binary), "unused", tmp_path / "absent.db", command) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_failed_refresh_retries_but_recovery_never_unpauses_admission(
    isolated_db: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Path(isolated_db)
    with db.connect(isolated_db):
        pass
    binary = tmp_path / "daemon"
    binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    binary.chmod(0o755)
    attempts = 0

    def refresh(binary: str, configuration: str, database: Path) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Synthetic unavailable replica")
        return {"status": "verified", "verified_at": time.time()}

    monkeypatch.setattr(backups, "refresh", refresh)
    monkeypatch.setattr(backups, "report_failure", lambda: None)
    monkeypatch.setattr(backups, "RETRY_SECONDS", 0.01)
    api = [sys.executable, "-c", "import time; time.sleep(1.5)"]
    assert backups.serve(str(binary), "unused", database, api) == 0
    assert attempts == 2
    assert read_control(db_path=isolated_db).paused
    assert read_control(db_path=isolated_db).revision == 1
    status = backups.read_status(database)
    assert status is not None and status["status"] == "verified"
