"""Restore a synthetic local Litestream replica and serve it offline."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from co_scientist.platform.db.backup_service import refresh


def isolated_environment(scratch: Path, database: Path) -> dict[str, str]:
    # An allowlist excludes provider/SMTP/telemetry credentials and dotenv files.
    return {
        "PATH": os.defpath,
        "HOME": str(scratch),
        "LANG": "C.UTF-8",
        "PYTHON_DOTENV_DISABLED": "1",
        "COSCIENTIST_DB_PATH": str(database),
        "COSCIENTIST_TEST_DOUBLE": "deterministic",
        "COSCIENTIST_LITESTREAM_ACTIVE": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "OTEL_SDK_DISABLED": "true",
    }


def stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=25)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def verify(database: Path) -> None:
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute(
            "SELECT value FROM restore_drill_marker"
        ).fetchall() == [("synthetic-only",)]


def run(litestream: Path, scratch: Path) -> dict[str, str | float | bool]:
    source = scratch / "source.db"
    restored = scratch / "restored.db"
    replica = scratch / "replica"
    environment = isolated_environment(scratch, source)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from co_scientist.platform.db import connect; "
            "c = connect(); db = c.__enter__(); "
            "db.execute('CREATE TABLE restore_drill_marker (value TEXT NOT NULL)'); "
            "db.execute(\"INSERT INTO restore_drill_marker VALUES ('synthetic-only')\"); "
            "c.__exit__(None, None, None)",
        ],
        cwd=scratch,
        env=environment,
        check=True,
        timeout=30,
    )
    configuration = scratch / "litestream.yml"
    configuration.write_text(
        f"dbs:\n  - path: {json.dumps(str(source))}\n"
        "    truncate-page-n: 0\n    replica:\n      type: file\n"
        f"      path: {json.dumps(str(replica))}\n",
        encoding="utf-8",
    )
    first = refresh(
        str(litestream), str(configuration), source, environment=environment
    )
    # Simulate an idle restore base old enough for the owner's 30-day R2 rule,
    # exclusively on a synthetic local file replica. No source DB write.
    snapshots = list((replica / "ltx" / "9").glob("*.ltx"))
    assert len(snapshots) == 1, "Expected one complete synthetic snapshot"
    old = time.time() - 31 * 86400
    os.utime(snapshots[0], (old, old))
    second = refresh(
        str(litestream), str(configuration), source, environment=environment
    )
    assert first["txid"] == second["txid"], "Idle source position changed"
    assert snapshots[0].stat().st_mtime > old + 30 * 86400
    with (scratch / "litestream.log").open("wb") as log:
        started = time.monotonic()
        subprocess.run(
            [
                str(litestream),
                "restore",
                "-txid",
                second["txid"],
                "-o",
                str(restored),
                replica.as_uri(),
            ],
            cwd=scratch,
            env=environment,
            stdout=log,
            stderr=log,
            check=True,
            timeout=60,
        )
    verify(restored)
    restore_seconds = time.monotonic() - started
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = isolated_environment(scratch, restored)
    with (scratch / "api.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "co_scientist.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--timeout-graceful-shutdown",
                "20",
            ],
            cwd=scratch,
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 60
            # Explicitly bypass proxy settings for the loopback readiness probe.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while True:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"API exited {process.returncode}; see {scratch / 'api.log'}"
                    )
                try:
                    with opener.open(
                        f"http://127.0.0.1:{port}/health", timeout=1
                    ) as response:
                        health = json.load(response)
                    if health["status"] == "healthy":
                        break
                except (urllib.error.URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"API did not become healthy; see {scratch / 'api.log'}"
                    )
                time.sleep(0.1)
            ready_seconds = time.monotonic() - started
        finally:
            stop(process)
    verify(restored)
    return {
        "scratch": str(scratch),
        "restore_seconds": round(restore_seconds, 3),
        "restore_to_healthy_seconds": round(ready_seconds, 3),
        "health": "healthy",
        "integrity": "ok",
        "data": "synthetic-only",
        "idle_refresh_verified": True,
        "idle_refresh_seconds": second["seconds"],
        "snapshot_txid": second["txid"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--litestream", required=True, type=Path)
    parser.add_argument("--scratch-parent", type=Path)
    arguments = parser.parse_args()
    litestream = arguments.litestream.resolve(strict=True)
    scratch = Path(
        tempfile.mkdtemp(prefix="cosci-restore-drill-", dir=arguments.scratch_parent)
    )
    result = run(litestream, scratch)
    (scratch / "receipt.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
