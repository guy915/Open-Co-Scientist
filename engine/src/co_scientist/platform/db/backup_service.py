"""Supervise one API and one Litestream daemon with verified daily snapshots."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

INTERVAL_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 60 * 60
COMMAND_TIMEOUT_SECONDS = 120
logger = logging.getLogger(__name__)


class Shutdown(BaseException):
    pass


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def command(arguments: list[str], *, environment: dict[str, str] | None = None) -> str:
    # Do not forward Litestream stderr: paths/endpoints can contain private
    # configuration. Return only structured metadata to the private verifier.
    result = subprocess.run(
        arguments,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Backup command failed")
    return result.stdout.decode("utf-8")


def refresh(
    binary: str,
    configuration: str,
    database: Path,
    *,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Caller must stop replication first; serving SQLite writers may continue."""
    started = time.time()
    elapsed = time.monotonic()
    common = ["-config", configuration]
    command(
        [binary, "replicate", *common, "-once", "-force-snapshot"],
        environment=environment,
    )
    with tempfile.TemporaryDirectory(prefix=".backup-verify-", dir=database.parent) as scratch:
        target = Path(scratch) / "restored.db"
        plan = json.loads(
            command(
                [
                    binary,
                    "restore",
                    *common,
                    "-dry-run",
                    "-json",
                    "-o",
                    str(target),
                    str(database),
                ],
                environment=environment,
            )
        )
        files = plan.get("files", [])
        # Verify the freshly uploaded complete base is actually selected for
        # recovery; a successful replay of old L0 files is insufficient.
        if not files or files[0]["level"] != 9:
            raise RuntimeError("Restore has no complete snapshot")
        snapshot_at = dt.datetime.fromisoformat(files[0]["timestamp"].replace("Z", "+00:00"))
        if snapshot_at.timestamp() < started - 10:
            raise RuntimeError("Restore snapshot was not refreshed")
        txid = plan["max_txid"]
        command(
            [
                binary,
                "restore",
                *common,
                "-txid",
                txid,
                "-o",
                str(target),
                str(database),
            ],
            environment=environment,
        )
        # No application import/startup mutates the verification copy. The
        # private temporary directory and matching sidecars are all removed.
        with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as connection:
            deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Restored snapshot failed integrity verification")
    return {
        "verified_at": time.time(),
        "snapshot_at": snapshot_at.timestamp(),
        "txid": txid,
        "seconds": round(time.monotonic() - elapsed, 3),
        "status": "verified",
    }


def status_path(database: Path) -> Path:
    return database.with_name(database.name + ".backup-status.json")


def read_status(database: Path) -> dict[str, Any] | None:
    try:
        with status_path(database).open("rb") as file:
            raw = file.read(4097)
        if len(raw) > 4096:
            return None
        value = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    state = value.get("status")
    if not isinstance(state, str) or state not in {"verified", "failed"}:
        return None
    result: dict[str, Any] = {"status": value["status"]}
    for field in ("verified_at", "snapshot_at", "failed_at", "seconds"):
        number = value.get(field)
        if (
            isinstance(number, int | float)
            and not isinstance(number, bool)
            and 0 <= number <= 10**12
            and math.isfinite(number)
        ):
            result[field] = number
    txid = value.get("txid")
    if isinstance(txid, str) and re.fullmatch(r"[0-9a-f]{16}", txid):
        result["txid"] = txid
    return result


def record_status(database: Path, status: dict[str, Any]) -> None:
    destination = status_path(database)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=database.parent, prefix=".backup-status-", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            json.dump(status, file)
            file.flush()
            os.fsync(file.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def pause_for_failure(database: Path) -> None:
    # Import only on an actual failure, after API startup. A deliberate
    # incident action writes once, never on a supervisor poll or public GET.
    from co_scientist.platform.db import transaction
    from co_scientist.platform.db.launch_control import read_control, write_control

    with transaction(str(database), durable=True) as connection:
        control = read_control(conn=connection)
        if not control.paused:
            write_control(
                connection,
                paused=True,
                drain=False,
                message="New research is paused while we verify backup recovery.",
                resumes_at=None,
                expected_revision=control.revision,
            )


def report_failure() -> None:
    # Emit one fixed error through the existing privacy projector when a DSN
    # is configured. Raw subprocess diagnostics and restored data stay local.
    from co_scientist.platform.telemetry.error_tracking import init_error_tracking

    init_error_tracking(os.getenv("SENTRY_DSN", ""), os.getenv("SENTRY_ENVIRONMENT", "production"))
    logger.error("Backup verification failed; new research paused")


def serve(binary: str, configuration: str, database: Path, api_command: list[str]) -> int:
    api = subprocess.Popen(api_command)
    replica: subprocess.Popen[bytes] | None = None
    due = 0.0
    last_success: dict[str, Any] = read_status(database) or {}
    try:
        while api.poll() is None:
            if replica is None:
                replica = subprocess.Popen(
                    [binary, "replicate", "-config", configuration],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            now = time.monotonic()
            if replica.poll() is not None:
                logger.error("Replication stopped; stopping the API")
                return 1
            ready = database.exists() and database.stat().st_size > 0
            if ready and now >= due:
                # Never run two Litestream writers for one replication state.
                stop(replica)
                replica = None
                try:
                    last_success = refresh(binary, configuration, database)
                    record_status(database, last_success)
                    due = time.monotonic() + INTERVAL_SECONDS
                except Exception:
                    record_status(
                        database,
                        {**last_success, "status": "failed", "failed_at": time.time()},
                    )
                    pause_for_failure(database)
                    report_failure()
                    due = time.monotonic() + RETRY_SECONDS
            time.sleep(1)
        return int(api.returncode or 0)
    finally:
        if replica is not None:
            stop(replica)
        stop(api)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--litestream", default="litestream")
    parser.add_argument("api_command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    api_command = arguments.api_command
    if api_command[:1] == ["--"]:
        api_command = api_command[1:]
    if not api_command:
        parser.error("An API command is required")
    database = Path(os.environ["COSCIENTIST_DB_PATH"]).resolve()

    def shutdown(signum: int, frame: object) -> None:
        raise Shutdown

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        raise SystemExit(serve(arguments.litestream, arguments.config, database, api_command))
    except Shutdown:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
