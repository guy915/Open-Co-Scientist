"""OS policy enforces the boundary; sessions coordinate operations."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from co_scientist.platform.llm.tool_effects import ToolEffect
from co_scientist.platform.sandbox import (
    METADATA_NAMES,
    workspace_write,
)
from co_scientist.platform.sandbox.cgroups import CommandCgroup
from co_scientist.platform.sandbox.patch import (
    PatchError,
    apply_patch,
    parse_patch,
    read_workspace_bytes,
    write_workspace_file,
)
from co_scientist.platform.sandbox.policy import SandboxPolicy
from co_scientist.platform.sandbox.runner import (
    _terminate,
    build_env,
    cleanup_cgroup,
    create_command_process,
)

logger = logging.getLogger(__name__)


# Bound each captured stream so endless output cannot become a memory leak.
MAX_SESSION_OUTPUT_BYTES = 1_000_000

# Bound live processes so a bad turn cannot leave unlimited orphans.
MAX_LIVE_SESSIONS = 4
MAX_PROCESS_SESSIONS = 16
MAX_SESSION_LIFETIME_SECONDS = 300.0
_process_sessions = threading.BoundedSemaphore(MAX_PROCESS_SESSIONS)


@dataclass
class _Stream:
    data: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def append(self, chunk: bytes) -> None:
        room = MAX_SESSION_OUTPUT_BYTES - len(self.data)
        if room <= 0:
            self.truncated = True
            return
        if len(chunk) > room:
            self.truncated = True
        self.data.extend(chunk[:room])

    def since(self, cursor: int) -> tuple[str, int]:
        start = max(0, min(cursor, len(self.data)))
        return (
            bytes(self.data[start:]).decode("utf-8", errors="replace"),
            len(self.data),
        )


@dataclass
class SessionRead:
    session_id: str
    running: bool
    exit_code: int | None
    stdout: str
    stderr: str
    cursor: dict[str, int]
    truncated: dict[str, bool]


class CommandSession:
    def __init__(self, session_id: str, argv: list[str]) -> None:
        self.id = session_id
        self.argv = list(argv)
        self._out = _Stream()
        self._err = _Stream()
        self._proc: asyncio.subprocess.Process | None = None
        self._cgroup: CommandCgroup | None = None
        self._pumps: list[asyncio.Task[None]] = []
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._lifecycle_error: str | None = None
        self._admitted = False

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def exit_code(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    async def start(
        self,
        *,
        policy: SandboxPolicy,
        cwd: Path,
        env: dict[str, str],
    ) -> None:
        if not _process_sessions.acquire(blocking=False):
            raise RuntimeError("process-wide command session limit reached")
        self._admitted = True
        try:
            self._proc, self._cgroup = await create_command_process(
                self.argv, policy, cwd=cwd, env=env
            )
        except BaseException:
            self._release_admission()
            raise
        self._pumps = [
            asyncio.create_task(self._pump(self._proc.stdout, self._out)),
            asyncio.create_task(self._pump(self._proc.stderr, self._err)),
        ]
        self._lifecycle_task = asyncio.create_task(self._reap_after_exit())

    async def _reap_after_exit(self) -> None:
        if self._proc is None:
            return
        try:
            try:
                await asyncio.wait_for(self._proc.wait(), MAX_SESSION_LIFETIME_SECONDS)
            except TimeoutError:
                group, self._cgroup = self._cgroup, None
                await _terminate(self._proc, group)
            if self._cgroup is not None:
                group, self._cgroup = self._cgroup, None
                try:
                    await cleanup_cgroup(group)
                except OSError as exc:
                    self._lifecycle_error = f"command cgroup cleanup failed: {exc}"
                    logger.exception("could not clean command cgroup for session %s", self.id)
        finally:
            self._release_admission()

    def _release_admission(self) -> None:
        if self._admitted:
            self._admitted = False
            _process_sessions.release()

    async def _pump(self, reader: object, into: _Stream) -> None:
        """Drain continuously: a full unread pipe blocks the writer between
        polls.
        """
        stream = reader
        while True:
            chunk = await stream.read(8192)  # type: ignore[attr-defined]
            if not chunk:
                return
            into.append(chunk)

    async def wait_for(self, seconds: float) -> None:
        if self._proc is None:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        await asyncio.gather(*self._pumps, return_exceptions=True)

    def read(self, cursor: dict[str, int] | None = None) -> SessionRead:
        marks = cursor or {}
        stdout, out_at = self._out.since(int(marks.get("stdout", 0)))
        stderr, err_at = self._err.since(int(marks.get("stderr", 0)))
        if self._lifecycle_error:
            stderr += f"\n{self._lifecycle_error}"
        return SessionRead(
            session_id=self.id,
            running=self.running,
            exit_code=self.exit_code,
            stdout=stdout,
            stderr=stderr,
            cursor={"stdout": out_at, "stderr": err_at},
            truncated={
                "stdout": self._out.truncated,
                "stderr": self._err.truncated,
            },
        )

    async def write(self, text: str) -> None:
        """Silently discarding input would claim a prompt was answered after
        the process exited.
        """
        if self._proc is None or self._proc.stdin is None or not self.running:
            raise ValueError(f"session {self.id} is not accepting input")
        self._proc.stdin.write(text.encode("utf-8"))
        await self._proc.stdin.drain()

    async def close(self) -> None:
        """Sessions outlive their starting calls; callers must reap them when a
        workspace closes.
        """
        try:
            if self._proc is not None:
                group, self._cgroup = self._cgroup, None
                await asyncio.shield(_terminate(self._proc, group))
        finally:
            for pump in self._pumps:
                pump.cancel()
            await asyncio.gather(*self._pumps, return_exceptions=True)
            if self._lifecycle_task is not None:
                await asyncio.gather(self._lifecycle_task, return_exceptions=True)


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, CommandSession] = {}
        self._starting = 0

    def get(self, session_id: str) -> CommandSession:
        return self._sessions[session_id]

    async def start(
        self,
        argv: list[str],
        *,
        policy: SandboxPolicy,
        cwd: Path,
        env_extra: dict[str, str] | None = None,
    ) -> CommandSession:
        self._make_room()
        if self._live_count() + self._starting >= MAX_LIVE_SESSIONS:
            raise RuntimeError(
                f"{MAX_LIVE_SESSIONS} commands are already running here; "
                f"wait for one to finish or end it first"
            )
        session = CommandSession(str(uuid.uuid4()), argv)
        self._starting += 1
        try:
            await session.start(
                policy=policy,
                cwd=cwd,
                env=build_env(extra={"TMPDIR": str(cwd), **(env_extra or {})}),
            )
            self._sessions[session.id] = session
        finally:
            self._starting -= 1
        return session

    def _live_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.running)

    def _make_room(self) -> None:
        """Retain finished exit status and final output while bounding process-
        local session history.
        """
        finished = [k for k, s in self._sessions.items() if not s.running]
        for key in finished[: max(0, len(finished) - MAX_LIVE_SESSIONS)]:
            del self._sessions[key]

    async def close(self) -> None:
        """Sessions outlive their starting calls; callers must reap them when a
        workspace closes.
        """
        closing = asyncio.create_task(self._close_all())
        cancelled = False
        while True:
            try:
                await asyncio.shield(closing)
                break
            except asyncio.CancelledError:
                if closing.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError()

    async def _close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()


# Bound commands so hangs cannot outlive durable lease renewal.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300.0


def _is_metadata(relative: Path) -> bool:
    return any(part in METADATA_NAMES for part in relative.parts)


@dataclass(frozen=True)
class PatchOutcome:
    changed: tuple[str, ...]
    rungs: dict[str, tuple[str, ...]]


class WorkspaceSession:
    """Skills are enabled per consumer, never merely by installation; measured
    effects differ by agent.
    """

    # PROCESS/WRITE effects are barriers; declare them beside operations before
    # registration.
    RUN_COMMAND_EFFECTS = ToolEffect.PROCESS | ToolEffect.READ
    APPLY_PATCH_EFFECTS = ToolEffect.WRITE | ToolEffect.READ
    READ_FILE_EFFECTS = ToolEffect.READ
    WRITE_FILE_EFFECTS = ToolEffect.WRITE | ToolEffect.READ

    def __init__(
        self,
        root: Path,
        *,
        policy: SandboxPolicy | None = None,
        network_allowed: bool = False,
        skills_enabled: bool = False,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.policy = policy or workspace_write(self.root, network_allowed=network_allowed)
        self.skills_enabled = skills_enabled
        self.sessions = SessionRegistry()

    async def close(self) -> None:
        """Sessions outlive their starting calls; callers must reap them when a
        workspace closes.
        """
        await self.sessions.close()

    def apply_patch_text(self, patch_text: str) -> PatchOutcome:
        result = apply_patch(parse_patch(patch_text), self.root)
        return PatchOutcome(changed=result.changed, rungs=result.rungs)

    def write_file(self, relative: str, content: str) -> None:
        """Whole-file creation avoids context-patch failures on a new program;
        patches retain edit anchoring.
        """
        write_workspace_file(self.root, relative, content)

    def read_file(self, relative: str, max_bytes: int = 200_000) -> str:
        try:
            raw = read_workspace_bytes(self.root, relative, max_bytes=max_bytes)
        except FileNotFoundError:
            raise PatchError(f"no such file in workspace: {relative!r}") from None
        except OSError as exc:
            raise PatchError(f"cannot read workspace file {relative!r}: {exc}") from exc
        return raw.decode("utf-8", errors="replace")

    def list_files(self) -> tuple[str, ...]:
        """Exclude harness metadata so the model cannot treat its own
        transcript as research input.
        """
        files = []
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            root_fd = os.open(self.root, directory_flags)
        except OSError:
            return ()

        def collect(directory_fd: int, parents: tuple[str, ...]) -> None:
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        parts = (*parents, entry.name)
                        relative = Path(*parts)
                        if _is_metadata(relative):
                            continue
                        try:
                            info = os.stat(
                                entry.name,
                                dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                        except OSError:
                            continue
                        if stat.S_ISREG(info.st_mode):
                            files.append(str(relative))
                        elif stat.S_ISDIR(info.st_mode):
                            try:
                                child_fd = os.open(
                                    entry.name,
                                    directory_flags,
                                    dir_fd=directory_fd,
                                )
                            except OSError:
                                continue
                            try:
                                collect(child_fd, parts)
                            finally:
                                os.close(child_fd)
            except OSError:
                # A command may remove or replace entries during a listing.
                return

        try:
            collect(root_fd, ())
        finally:
            os.close(root_fd)
        return tuple(sorted(files))
