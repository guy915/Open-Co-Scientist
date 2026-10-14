"""Own confined command trees with cgroups and rebuild command environments.
Resource controls are installed by a trusted launcher before execution.
"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from co_scientist.platform.sandbox.argv import wrap_argv
from co_scientist.platform.sandbox.cgroups import CommandCgroup, launcher_argv
from co_scientist.platform.sandbox.cgroups import available as cgroups_available
from co_scientist.platform.sandbox.cgroups import create as create_cgroup
from co_scientist.platform.sandbox.confine_exec import policy_to_json
from co_scientist.platform.sandbox.policy import SandboxPolicy

logger = logging.getLogger(__name__)

# PATH/HOME/locale/temp variables support runtimes; credentials require explicit
# per-call injection.
DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
)

_SIGTERM_GRACE_SECONDS = 5.0

DEFAULT_MAX_OUTPUT_BYTES = 1_000_000


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def build_env(
    allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Never inherit provider/cloud credentials implicitly; callers name extra
    values per command.
    """
    env = {name: os.environ[name] for name in allowlist if name in os.environ}
    if extra:
        env.update(extra)
    return env


def _decode(raw: bytes, limit: int) -> tuple[str, bool]:
    truncated = len(raw) > limit
    body = raw[:limit] if truncated else raw
    return body.decode("utf-8", errors="replace"), truncated


async def _terminate(proc: asyncio.subprocess.Process, cgroup: CommandCgroup | None = None) -> None:
    """Children can hold pipes after their parent exits; kill the group or
    output reads can hang.
    """
    if proc.returncode is not None:
        if cgroup is not None:
            await cleanup_cgroup(cgroup)
        return
    if cgroup is not None:
        cgroup.kill()
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        if cgroup is not None:
            await cleanup_cgroup(cgroup)
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=_SIGTERM_GRACE_SECONDS)
        if cgroup is not None:
            await cleanup_cgroup(cgroup)
        return
    except asyncio.TimeoutError:
        logger.warning("confined command %s ignored SIGTERM; sending SIGKILL", proc.pid)

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if cgroup is not None:
            await cleanup_cgroup(cgroup)
        return
    await proc.wait()
    if cgroup is not None:
        await cleanup_cgroup(cgroup)


async def cleanup_cgroup(cgroup: CommandCgroup) -> None:
    cgroup.kill()
    events = cgroup.path / "cgroup.events"
    for _ in range(100):
        try:
            populated = "populated 1" in events.read_text(encoding="ascii")
        except OSError:
            populated = False
        if not populated:
            break
        await asyncio.sleep(0.05)
    cgroup.remove()


@dataclass(frozen=True)
class ExecRequest:
    argv: list[str]
    policy: SandboxPolicy
    cwd: Path | None = None
    timeout_seconds: float | None = 300.0
    env: dict[str, str] | None = None
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES


async def run_sandboxed(request: ExecRequest) -> ExecResult:
    """Timeouts remain data so scored callers can distinguish them without
    spending a retry.
    """
    wrapped = wrap_argv(request.argv, request.policy)
    cgroup: CommandCgroup | None = None
    if request.policy.confines_in_process:
        cgroup = create_cgroup()
        if cgroup is None:
            raise RuntimeError("no delegated cgroup v2 lifecycle boundary is available")
        wrapped = launcher_argv(cgroup, policy_to_json(request.policy), request.argv)
    env = request.env if request.env is not None else build_env()

    try:
        proc = await asyncio.create_subprocess_exec(
            *wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(request.cwd) if request.cwd else None,
            env=env,
            start_new_session=True,
        )
    except BaseException:
        if cgroup is not None:
            cgroup.remove()
        raise

    return await _collect(proc, request, cgroup)


async def _collect(
    proc: asyncio.subprocess.Process,
    request: ExecRequest,
    cgroup: CommandCgroup | None = None,
) -> ExecResult:
    timed_out = False
    try:
        try:
            raw_out, raw_err = await asyncio.wait_for(
                proc.communicate(), timeout=request.timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            await _terminate(proc, cgroup)
            cgroup = None
            raw_out, raw_err = b"", b""
            logger.warning(
                "confined command timed out after %ss: %s",
                request.timeout_seconds,
                request.argv[0],
            )
        stdout, out_cut = _decode(raw_out or b"", request.max_output_bytes)
        stderr, err_cut = _decode(raw_err or b"", request.max_output_bytes)
    except asyncio.CancelledError:
        await asyncio.shield(_terminate(proc, cgroup))
        cgroup = None
        raise
    finally:
        if cgroup is not None:
            await asyncio.shield(cleanup_cgroup(cgroup))
    return ExecResult(
        exit_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        truncated=out_cut or err_cut,
    )


async def create_command_process(
    argv: list[str],
    policy: SandboxPolicy,
    *,
    cwd: Path,
    env: dict[str, str],
) -> tuple[asyncio.subprocess.Process, CommandCgroup | None]:
    """Start a persistent command inside its own confined lifecycle boundary."""
    cgroup = create_cgroup() if policy.confines_in_process else None
    if policy.confines_in_process and cgroup is None:
        raise RuntimeError("no delegated cgroup v2 lifecycle boundary is available")
    command = (
        launcher_argv(cgroup, policy_to_json(policy), argv)
        if cgroup is not None
        else wrap_argv(argv, policy)
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
        )
    except BaseException:
        if cgroup is not None:
            cgroup.remove()
        raise
    return proc, cgroup


def command_lifecycle_available() -> bool:
    return cgroups_available()
