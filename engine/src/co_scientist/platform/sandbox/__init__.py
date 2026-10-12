"""Unsupported confinement must fail closed, never return an unconfined
command.
"""

from co_scientist.platform.sandbox.argv import (
    UnsupportedSandboxError,
    sandbox_backend,
    wrap_argv,
)
from co_scientist.platform.sandbox.command_safety import is_known_safe
from co_scientist.platform.sandbox.policy import (
    HARNESS_METADATA_NAME,
    METADATA_NAMES,
    PROTECTED_METADATA_NAMES,
    SandboxKind,
    SandboxPolicy,
    read_only,
    workspace_write,
)
from co_scientist.platform.sandbox.runner import (
    DEFAULT_ENV_ALLOWLIST,
    ExecRequest,
    ExecResult,
    build_env,
    command_lifecycle_available,
    run_sandboxed,
)

__all__ = [
    "DEFAULT_ENV_ALLOWLIST",
    "HARNESS_METADATA_NAME",
    "METADATA_NAMES",
    "PROTECTED_METADATA_NAMES",
    "ExecRequest",
    "ExecResult",
    "SandboxKind",
    "SandboxPolicy",
    "UnsupportedSandboxError",
    "build_env",
    "command_lifecycle_available",
    "is_known_safe",
    "read_only",
    "run_sandboxed",
    "sandbox_backend",
    "workspace_write",
    "wrap_argv",
]
