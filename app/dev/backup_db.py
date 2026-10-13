"""Create a consistent, private SQLite backup without serving-process writes."""

import argparse
import math
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path


def _copy_database(source: Path, target: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("SQLite backup exceeded its deadline")

    with (
        closing(
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        ) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst, pages=128, progress=progress, sleep=0.01)
        if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite backup failed integrity verification")


def backup_database(
    source: Path, destination: Path, timeout: float = 30.0
) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(fd)
    target = Path(temporary)
    try:
        _copy_database(source, target, timeout)
        # Hard-link publication cannot replace a target another backup created
        # during the copy.
        os.link(target, destination)
    finally:
        target.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    backup_database(args.source, args.destination, args.timeout)
    print(f"Validated SQLite backup: {args.destination}")


if __name__ == "__main__":
    main()
