from __future__ import annotations

from dataclasses import dataclass

from co_scientist.platform.db import Connection, use_conn


@dataclass(frozen=True)
class LaunchControl:
    paused: bool = False
    drain: bool = False
    message: str = "Research is temporarily paused."
    resumes_at: float | None = None
    revision: int = 0
    drain_generation: int = 0


class LaunchPausedError(Exception):
    def __init__(self, control: LaunchControl) -> None:
        self.control = control
        super().__init__(control.message)


class RevisionConflictError(Exception):
    pass


def read_control(*, conn: Connection | None = None, db_path: str | None = None) -> LaunchControl:
    with use_conn(conn, db_path) as active:
        row = active.execute("SELECT * FROM launch_control WHERE id=1").fetchone()
    if row is None:
        return LaunchControl()
    return LaunchControl(
        bool(row["paused"]),
        bool(row["drain"]),
        row["message"],
        row["resumes_at"],
        row["revision"],
        row["drain_generation"],
    )


def require_unpaused(*, conn: Connection | None = None) -> LaunchControl:
    control = read_control(conn=conn)
    if control.paused:
        raise LaunchPausedError(control)
    return control


def write_control(
    conn: Connection,
    *,
    paused: bool,
    drain: bool,
    message: str,
    resumes_at: float | None,
    expected_revision: int,
) -> LaunchControl:
    previous = read_control(conn=conn)
    if previous.revision != expected_revision:
        raise RevisionConflictError
    conn.execute(
        "INSERT INTO launch_control VALUES (1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "paused=excluded.paused,drain=excluded.drain,message=excluded.message,"
        "resumes_at=excluded.resumes_at,revision=excluded.revision,"
        "drain_generation=excluded.drain_generation",
        (
            int(paused),
            int(drain),
            message,
            resumes_at,
            previous.revision + 1,
            previous.drain_generation + int(paused and drain),
        ),
    )
    return read_control(conn=conn)
