"""Unavailable or inexpressible confinement must refuse execution."""

import sys
from collections.abc import Callable

from co_scientist.platform.sandbox import landlock, seatbelt
from co_scientist.platform.sandbox.policy import SandboxPolicy


class UnsupportedSandboxError(RuntimeError):
    """Confinement failure must propagate rather than degrade to unconfined
    execution.
    """


def sandbox_backend() -> str | None:
    """Landlock needs no privileges but cannot carve out metadata."""
    if sys.platform == "darwin":
        return "seatbelt"
    if not sys.platform.startswith("linux"):
        return None
    if landlock.is_available():
        return "landlock"
    return None


def _landlock_argv(argv: list[str], policy: SandboxPolicy) -> list[str]:
    """Apply process-local Landlock/seccomp in a helper that execs, never a
    threaded preexec_fn.
    """
    from co_scientist.platform.sandbox.confine_exec import policy_to_json

    return [
        sys.executable,
        "-I",
        "-m",
        "co_scientist.platform.sandbox.confine_exec",
        policy_to_json(policy),
        "--",
        *argv,
    ]


_BACKEND_WRAPPERS: dict[str, Callable[[list[str], SandboxPolicy], list[str]]] = {
    "seatbelt": seatbelt.wrap_argv,
    "landlock": _landlock_argv,
}


def wrap_argv(argv: list[str], policy: SandboxPolicy) -> list[str]:
    if not argv:
        raise ValueError("cannot wrap an empty command")
    if not policy.confines_in_process:
        return list(argv)

    wrapper = _BACKEND_WRAPPERS.get(sandbox_backend() or "")
    if wrapper is not None:
        return wrapper(argv, policy)

    raise UnsupportedSandboxError(
        f"no sandbox backend available on platform {sys.platform!r}: "
        f"refusing to run {argv[0]!r} unconfined. Landlock needs Linux "
        f"6.2 or newer (ABI 3); failing that, run the "
        f"command under an external sandbox and pass SandboxKind.EXTERNAL."
    )
