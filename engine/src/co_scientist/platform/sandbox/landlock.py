"""Landlock needs no privileges but grants only additive filesystem access.
TCP-only Landlock denial leaves UDP open; seccomp handles network denial.
"""

import ctypes
import logging
import os
import stat
import struct
from pathlib import Path

from co_scientist.platform.sandbox.policy import (
    PROTECTED_METADATA_NAMES,
    SandboxKind,
    SandboxPolicy,
)

logger = logging.getLogger(__name__)

# These syscall numbers match x86_64 and aarch64.
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446

_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

# O_PATH is Linux-only but this module must import on macOS; its Linux value is
# architecture-independent.
_O_PATH = getattr(os, "O_PATH", 0o010000000)

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REFER = 1 << 13
_TRUNCATE = 1 << 14

_ABI1_ALL = (1 << 13) - 1

# No REFER grant outside writable roots: refuse cross-root renames.
_READ_ACCESS = _EXECUTE | _READ_FILE | _READ_DIR


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def abi_version() -> int | None:
    try:
        libc = _libc()
    except OSError:  # pragma: no cover - libc is always present on Linux
        return None
    result = libc.syscall(
        _NR_CREATE_RULESET,
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint32(_CREATE_RULESET_VERSION),
    )
    return result if result > 0 else None


def is_available() -> bool:
    return (abi_version() or 0) >= 3


def _handled_access(abi: int) -> int:
    """Handle REFER to permit renames within writable roots; leave terminal
    IOCTL_DEV unhandled.
    """
    handled = _ABI1_ALL
    if abi >= 2:
        handled |= _REFER
    if abi >= 3:
        handled |= _TRUNCATE
    return handled


def _ruleset_attr(abi: int, handled: int) -> ctypes.Array[ctypes.c_char]:
    """The kernel rejects unknown structure sizes; select the size by Landlock
    ABI.
    """
    if abi >= 6:
        return ctypes.create_string_buffer(struct.pack("=QQQ", handled, 0, 0), 24)
    if abi >= 4:
        return ctypes.create_string_buffer(struct.pack("=QQ", handled, 0), 16)
    return ctypes.create_string_buffer(struct.pack("=Q", handled), 8)


def _add_path_rule(libc: ctypes.CDLL, ruleset_fd: int, path: Path, access: int) -> None:
    parent_fd = os.open(path, _O_PATH | os.O_CLOEXEC)
    try:
        # Landlock rejects directory-only rights on files, including runtime
        # config files and device nodes. Inspect the opened inode, not its path.
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            access &= _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE
        # landlock_path_beneath_attr is packed: 8+4 bytes, not 16.
        attr = ctypes.create_string_buffer(struct.pack("=Qi", access, parent_fd), 12)
        if libc.syscall(
            _NR_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_uint32(_RULE_PATH_BENEATH),
            attr,
            ctypes.c_uint32(0),
        ):
            errno = ctypes.get_errno()
            raise OSError(errno, f"landlock_add_rule({path}) failed")
    finally:
        os.close(parent_fd)


def unenforceable_roots(policy: SandboxPolicy) -> tuple[Path, ...]:
    """Additive rules cannot protect metadata inside an otherwise writable
    root.
    """
    offending = []
    for root in policy.writable_roots:
        if any((root / name).exists() for name in PROTECTED_METADATA_NAMES):
            offending.append(root)
    return tuple(offending)


def can_enforce(policy: SandboxPolicy) -> bool:
    return not unenforceable_roots(policy)


def restrict_self(policy: SandboxPolicy) -> None:
    """Restrictions survive execve and commit only at the final
    landlock_restrict_self.
    """
    if policy.kind is SandboxKind.DANGER_FULL_ACCESS:
        return
    abi = abi_version()
    if abi is None or abi < 3:
        raise OSError("Landlock ABI 3 or newer is required for truncation confinement")

    libc = _libc()
    handled = _handled_access(abi)
    ruleset_fd = libc.syscall(
        _NR_CREATE_RULESET,
        _ruleset_attr(abi, handled),
        ctypes.c_size_t(len(_ruleset_attr(abi, handled))),
        ctypes.c_uint32(0),
    )
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")

    try:
        for root in policy.readable_roots:
            _add_path_rule(libc, ruleset_fd, root, _READ_ACCESS)
        for root in policy.writable_roots:
            _add_path_rule(libc, ruleset_fd, root, handled)
        _commit(libc, ruleset_fd)
    finally:
        os.close(ruleset_fd)


def _commit(libc: ctypes.CDLL, ruleset_fd: int) -> None:
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.syscall(_NR_RESTRICT_SELF, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0)):
        raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
