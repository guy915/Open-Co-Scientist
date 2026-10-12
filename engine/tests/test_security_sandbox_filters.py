from __future__ import annotations

import json
import platform
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from co_scientist.platform.sandbox import landlock, seccomp, workspace_write
from co_scientist.platform.sandbox.argv import _landlock_argv, sandbox_backend


@pytest.mark.parametrize("abi", [None, 1, 2, 3, 4])
def test_truncation_confinement_is_required_in_probe_and_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, abi: int | None
) -> None:
    monkeypatch.setattr(landlock, "abi_version", lambda: abi)
    assert landlock.is_available() is (abi is not None and abi >= 3)
    if abi is None or abi < 3:
        with pytest.raises(OSError, match="ABI 3"):
            landlock.restrict_self(workspace_write(tmp_path))


def _evaluate(program: bytes, arch: int, syscall: int) -> int:
    instructions = [struct.unpack("=HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
    pc = accumulator = 0
    while True:
        code, yes, no, value = instructions[pc]
        if code == 0x20:
            accumulator = arch if value == 4 else syscall
        elif code == 0x54:
            accumulator &= value
        elif code == 0x15:
            pc += yes if accumulator == value else no
        elif code == 0x06:
            return int(value)
        else:
            raise AssertionError(code)
        pc += 1


@pytest.mark.parametrize("machine", ["x86_64", "aarch64", "arm64"])
def test_no_network_installs_ring_denial_for_each_supported_architecture(
    monkeypatch: pytest.MonkeyPatch, machine: str
) -> None:
    programs: list[bytes] = []
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(seccomp, "_install", programs.append)
    seccomp.deny_network()
    assert len(programs) == 2
    arch = seccomp._ARCHITECTURES[machine][0]
    for syscall in (425, 426, 427, 425 | 0x40000000):
        assert _evaluate(programs[0], arch, syscall) == seccomp._RET_ERRNO_EPERM
    assert _evaluate(programs[0], arch, 0) == seccomp._RET_ALLOW
    assert _evaluate(programs[0], 0, 425) == seccomp._RET_ERRNO_EPERM


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux seccomp only")
def test_installed_filter_blocks_internet_and_keeps_local_ipc_and_files(tmp_path: Path) -> None:
    code = """
import ctypes, json, socket
from co_scientist.platform.sandbox.seccomp import deny_network
deny_network()
results = []
for family in (socket.AF_INET, socket.AF_INET6):
    try:
        socket.socket(family).close()
    except OSError as exc:
        results.append(exc.errno)
socket.socket(socket.AF_UNIX).close()
libc = ctypes.CDLL(None, use_errno=True)
for syscall in (425, 426, 427):
    assert libc.syscall(syscall, 0, 0, 0, 0, 0, 0) == -1
    assert ctypes.get_errno() == 1
print(json.dumps(results))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [13, 13]


@pytest.mark.skipif(sandbox_backend() != "landlock", reason="Landlock ABI 3 unavailable")
def test_production_wrapper_blocks_host_truncate_and_allows_workspace_truncate(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "private.txt"
    outside.write_text("must remain intact")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "data.txt"
    inside.write_text("allowed")
    code = """
import os, sys
try:
    os.truncate(sys.argv[1], 0)
except PermissionError:
    pass
else:
    raise AssertionError('outside truncation succeeded')
os.truncate(sys.argv[2], 0)
"""
    result = subprocess.run(
        _landlock_argv(
            [sys.executable, "-c", code, str(outside), str(inside)], workspace_write(workspace)
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert outside.read_text() == "must remain intact"
    assert inside.read_text() == ""
