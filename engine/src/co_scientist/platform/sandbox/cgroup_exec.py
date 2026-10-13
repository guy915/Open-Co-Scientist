"""Join the trusted cgroup before applying OS confinement and execing code."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

from co_scientist.platform.sandbox.confine_exec import _apply, policy_from_json


def _fail(message: str) -> NoReturn:
    print(f"cgroup_exec: {message}", file=sys.stderr)
    raise SystemExit(124)


def main(argv: list[str]) -> None:
    if "--" not in argv:
        _fail("expected <cgroup-path> <policy-json> -- <command>")
    separator = argv.index("--")
    if separator != 2 or not argv[3:]:
        _fail("expected <cgroup-path> <policy-json> -- <command>")
    group = Path(argv[0])
    try:
        policy = policy_from_json(argv[1])
        (group / "cgroup.procs").write_text(str(os.getpid()), encoding="ascii")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _fail(f"could not join command cgroup: {exc}")
    _apply(policy)
    try:
        os.execvp(argv[3], argv[3:])
    except OSError as exc:
        _fail(f"could not exec {argv[3]}: {exc}")


if __name__ == "__main__":
    main(sys.argv[1:])
