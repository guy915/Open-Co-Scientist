"""Deny internet socket families to cover UDP as well as TCP. AF_UNIX remains
available for local runtime IPC.
"""

import ctypes
import logging
import platform
import struct

logger = logging.getLogger(__name__)

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2

_RET_ALLOW = 0x7FFF0000
_RET_ERRNO_EACCES = 0x00050000 | 13
_RET_ERRNO_EPERM = 0x00050000 | 1

_OFFSET_NR = 0
_OFFSET_ARCH = 4
_OFFSET_ARG0 = 16

_LD_W_ABS = 0x20
_JEQ_K = 0x15
_ALU_AND_K = 0x54
_RET_K = 0x06

_AF_INET = 2
_AF_INET6 = 10
_AF_PACKET = 17

_ARCHITECTURES = {
    "aarch64": (0xC00000B7, 198, (117, 270, 271, 438)),
    "arm64": (0xC00000B7, 198, (117, 270, 271, 438)),
    "x86_64": (0xC000003E, 41, (101, 310, 311, 438)),
}


class SeccompUnavailableError(OSError):
    """Failure to install the filter must never permit unconfined network
    access.
    """


def _instruction(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def _deny_inet_program(audit_arch: int, socket_nr: int) -> bytes:
    """BPF jumps are relative to the next instruction; mismatches must reach
    allow, not deny.
    """
    return b"".join(
        [
            _instruction(_LD_W_ABS, 0, 0, _OFFSET_ARCH),
            _instruction(_JEQ_K, 0, 6, audit_arch),
            _instruction(_LD_W_ABS, 0, 0, _OFFSET_NR),
            _instruction(_JEQ_K, 0, 4, socket_nr),
            _instruction(_LD_W_ABS, 0, 0, _OFFSET_ARG0),
            _instruction(_JEQ_K, 3, 0, _AF_INET),
            _instruction(_JEQ_K, 2, 0, _AF_INET6),
            _instruction(_JEQ_K, 1, 0, _AF_PACKET),
            _instruction(_RET_K, 0, 0, _RET_ALLOW),
            _instruction(_RET_K, 0, 0, _RET_ERRNO_EACCES),
        ]
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.c_void_p))


def deny_network() -> None:
    architecture = _ARCHITECTURES.get(platform.machine())
    if architecture is None:
        raise SeccompUnavailableError(f"no seccomp filter for machine {platform.machine()!r}")

    program = _deny_inet_program(*architecture[:2])
    # Ring operations execute inside the kernel without traversing socket's
    # syscall filter. These numbers match both supported architectures.
    _install(_deny_syscalls_program(architecture[0], (425, 426, 427)))
    _install(program)


def deny_process_inspection() -> None:
    """Block cross-process memory and descriptor reads that bypass file rules."""
    architecture = _ARCHITECTURES.get(platform.machine())
    if architecture is None:
        raise SeccompUnavailableError(f"no seccomp filter for machine {platform.machine()!r}")
    _install(_deny_syscalls_program(architecture[0], architecture[2]))


def _deny_syscalls_program(audit_arch: int, syscalls: tuple[int, ...]) -> bytes:
    instructions = [
        _instruction(_LD_W_ABS, 0, 0, _OFFSET_ARCH),
        _instruction(_JEQ_K, 1, 0, audit_arch),
        _instruction(_RET_K, 0, 0, _RET_ERRNO_EPERM),
        _instruction(_LD_W_ABS, 0, 0, _OFFSET_NR),
        _instruction(_ALU_AND_K, 0, 0, 0x40000000),
        _instruction(_JEQ_K, 1, 0, 0),
        _instruction(_RET_K, 0, 0, _RET_ERRNO_EPERM),
        _instruction(_LD_W_ABS, 0, 0, _OFFSET_NR),
    ]
    for syscall in syscalls:
        instructions.extend(
            (
                _instruction(_JEQ_K, 0, 1, syscall),
                _instruction(_RET_K, 0, 0, _RET_ERRNO_EPERM),
            )
        )
    instructions.append(_instruction(_RET_K, 0, 0, _RET_ALLOW))
    return b"".join(instructions)


def _install(program: bytes) -> None:
    blob = ctypes.create_string_buffer(program, len(program))
    fprog = _SockFprog(len(program) // 8, ctypes.cast(blob, ctypes.c_void_p))

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
        raise SeccompUnavailableError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0):
        raise SeccompUnavailableError(ctypes.get_errno(), "PR_SET_SECCOMP failed")
