"""Disposable cgroup v2 ownership for generated command trees."""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

CGROUP_ROOT_ENV = "COSCIENTIST_CGROUP_ROOT"


def _root() -> Path | None:
    configured = os.getenv(CGROUP_ROOT_ENV)
    if not configured:
        return None
    root = Path(configured).resolve()
    if not (root / "cgroup.controllers").is_file():
        return None
    return root


@dataclass
class CommandCgroup:
    path: Path

    def kill(self) -> None:
        kill_file = self.path / "cgroup.kill"
        kill_file.write_text("1", encoding="ascii")

    def remove(self) -> None:
        self.path.rmdir()


def create() -> CommandCgroup | None:
    root = _root()
    if root is None:
        return None
    path = root / f"command-{uuid.uuid4().hex}"
    try:
        path.mkdir()
        required = ("cgroup.procs", "cgroup.kill", "memory.max", "pids.max", "cpu.max")
        if any(not (path / name).is_file() for name in required):
            path.rmdir()
            return None
        (path / "memory.max").write_text("536870912", encoding="ascii")
        (path / "pids.max").write_text("64", encoding="ascii")
        (path / "cpu.max").write_text("100000 100000", encoding="ascii")
        (path / "cgroup.kill").write_text("1", encoding="ascii")
    except OSError:
        try:
            (path / "cgroup.kill").write_text("1", encoding="ascii")
            path.rmdir()
        except OSError:
            pass
        return None
    return CommandCgroup(path)


def available() -> bool:
    probe = create()
    if probe is None:
        return False
    try:
        probe.remove()
    except OSError:
        return False
    return not probe.path.exists()


def launcher_argv(group: CommandCgroup, policy_json: str, argv: list[str]) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-m",
        "co_scientist.platform.sandbox.cgroup_exec",
        str(group.path),
        policy_json,
        "--",
        *argv,
    ]
