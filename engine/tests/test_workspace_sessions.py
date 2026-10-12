from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import co_scientist.platform.sandbox.patch as patch_module
from co_scientist.platform.sandbox import (
    HARNESS_METADATA_NAME,
    METADATA_NAMES,
    PROTECTED_METADATA_NAMES,
    command_lifecycle_available,
    sandbox_backend,
)
from co_scientist.platform.sandbox.workspace import (
    LIST_FILES,
    MIN_SECRET_LENGTH,
    READ_FILE,
    SPILL_DIRECTORY,
    OutputRecorder,
    SecretRegistrationError,
    SecretRegistry,
    WorkspaceSession,
    WorkspaceToolProvider,
)
from co_scientist.platform.sandbox.workspace import RUN_COMMAND as _WORKSPACE_OUTPUT_RUN_COMMAND
from co_scientist.platform.sandbox.workspace import output as workspace_output
from co_scientist.platform.sandbox.workspace.session import (
    MAX_SESSION_OUTPUT_BYTES,
    SessionRegistry,
)
from co_scientist.platform.sandbox.workspace.tool_schemas import POLL_COMMAND
from co_scientist.platform.sandbox.workspace.tool_schemas import (
    RUN_COMMAND as _WORKSPACE_SESSIONS_RUN_COMMAND,
)
from tests._llm_fake import make_tool_call

_requires_sandbox = pytest.mark.skipif(
    sandbox_backend() is None or not command_lifecycle_available(),
    reason="no filesystem and lifecycle sandbox on this platform",
)


def _workspace_sessions_call(name: str, arguments: Any) -> SimpleNamespace:
    return make_tool_call(f"call_{name}", name, json.dumps(arguments))


def _owned_process_groups(registry: SessionRegistry) -> set[int]:
    roots = {
        session._proc.pid
        for session in registry._sessions.values()
        if session._proc is not None and session._proc.returncode is None
    }
    if not roots:
        return set()
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid=,pgid="], text=True)
    processes = [tuple(map(int, row.split())) for row in rows.splitlines()]
    descendants = set(roots)
    while True:
        found = {pid for pid, parent, _ in processes if parent in descendants}
        if found <= descendants:
            break
        descendants.update(found)
    return {group for pid, _, group in processes if pid in descendants}


def _reap_process_groups(groups: set[int]) -> None:
    # Confined children may start their own session and inherit the pipes.
    for group in groups:
        with suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)


async def _execute(provider: WorkspaceToolProvider, name: str, **arguments: Any) -> dict[str, Any]:
    groups = _owned_process_groups(provider.session.sessions) if arguments.get("kill") else set()
    try:
        message = await provider.execute_tool_call(_workspace_sessions_call(name, arguments))
    finally:
        _reap_process_groups(groups)
    parsed: dict[str, Any] = json.loads(message["content"])
    return parsed


async def _wait_for_output(
    provider: WorkspaceToolProvider,
    payload: dict[str, Any],
    needle: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """A loaded host or sandbox re-exec can outlast a yield window; bounded
    polling resolves that race."""
    deadline = time.monotonic() + timeout
    while needle not in payload["stdout"]:
        if time.monotonic() >= deadline:
            raise AssertionError(f"{needle!r} did not appear in stdout within {timeout}s")
        payload = await _execute(
            provider,
            POLL_COMMAND,
            session_id=payload["session_id"],
            cursor=payload["cursor"],
            wait_seconds=0.5,
        )
    return payload


async def _close_registry(registry: SessionRegistry) -> None:
    processes = [
        session._proc for session in registry._sessions.values() if session._proc is not None
    ]
    groups = _owned_process_groups(registry)
    try:
        await registry.close()
    finally:
        _reap_process_groups(groups)
        for process in processes:
            await process.communicate()


@pytest.fixture
async def provider(tmp_path: Path) -> AsyncIterator[WorkspaceToolProvider]:
    value = WorkspaceToolProvider(WorkspaceSession(tmp_path))
    try:
        yield value
    finally:
        await _close_registry(value.session.sessions)


@_requires_sandbox
class TestStillRunningIsAnAnswer:
    async def test_a_slow_command_comes_back_with_a_session(
        self, provider: WorkspaceToolProvider
    ) -> None:
        payload = await _execute(
            provider,
            _WORKSPACE_SESSIONS_RUN_COMMAND,
            argv=["bash", "-c", "echo early; sleep 30"],
            yield_seconds=0.4,
        )
        assert payload["running"] is True
        assert payload["session_id"]
        payload = await _wait_for_output(provider, payload, "early")
        assert "early" in payload["stdout"]
        await _close_registry(provider.session.sessions)

    async def test_a_fast_command_finishes_in_one_call(
        self, provider: WorkspaceToolProvider
    ) -> None:
        payload = await _execute(
            provider,
            _WORKSPACE_SESSIONS_RUN_COMMAND,
            argv=["bash", "-lc", "echo hi"],
        )
        assert payload["running"] is False
        assert payload["exit_code"] == 0
        assert "hi" in payload["stdout"]

    async def test_polling_carries_it_to_its_exit_code(
        self, provider: WorkspaceToolProvider
    ) -> None:
        started = await _execute(
            provider,
            _WORKSPACE_SESSIONS_RUN_COMMAND,
            argv=["bash", "-lc", "sleep 0.3; echo done; exit 7"],
            yield_seconds=0.05,
        )
        polled = await _execute(
            provider,
            POLL_COMMAND,
            session_id=started["session_id"],
            cursor=started["cursor"],
            wait_seconds=10,
        )
        assert polled["running"] is False
        assert polled["exit_code"] == 7
        assert "done" in polled["stdout"]

    async def test_the_cursor_does_not_repeat_output(self, provider: WorkspaceToolProvider) -> None:
        # Cursor advancement prevents quadratic replay; gating the second line
        # removes yield races.
        started = await _execute(
            provider,
            _WORKSPACE_SESSIONS_RUN_COMMAND,
            argv=["bash", "-c", "echo first; read line; echo second"],
            yield_seconds=0.1,
        )
        started = await _wait_for_output(provider, started, "first")
        assert "first" in started["stdout"]
        polled = await _execute(
            provider,
            POLL_COMMAND,
            session_id=started["session_id"],
            cursor=started["cursor"],
            input="go\n",
            wait_seconds=10,
        )
        assert "second" in polled["stdout"]
        assert "first" not in polled["stdout"]


@_requires_sandbox
class TestDrivingIt:
    async def test_input_reaches_a_waiting_command(self, provider: WorkspaceToolProvider) -> None:
        started = await _execute(
            provider,
            _WORKSPACE_SESSIONS_RUN_COMMAND,
            argv=["bash", "-lc", "read line; echo got:$line"],
            yield_seconds=0.05,
        )
        polled = await _execute(
            provider,
            POLL_COMMAND,
            session_id=started["session_id"],
            cursor=started["cursor"],
            input="hello\n",
            wait_seconds=10,
        )
        assert "got:hello" in polled["stdout"]

    async def test_kill_ends_it(self, provider: WorkspaceToolProvider) -> None:
        started = await _execute(
            provider,
            _WORKSPACE_SESSIONS_RUN_COMMAND,
            argv=["bash", "-lc", "sleep 60"],
            yield_seconds=0.05,
        )
        killed = await _execute(
            provider,
            POLL_COMMAND,
            session_id=started["session_id"],
            kill=True,
        )
        assert killed["running"] is False

    async def test_input_to_a_finished_command_is_refused_legibly(
        self, provider: WorkspaceToolProvider
    ) -> None:
        """Invalid model input is a tool error, not an exception that fails
        sibling calls."""
        started = await _execute(
            provider,
            _WORKSPACE_SESSIONS_RUN_COMMAND,
            argv=["bash", "-lc", "read line; echo got:$line"],
            yield_seconds=0.05,
        )
        await _execute(
            provider,
            POLL_COMMAND,
            session_id=started["session_id"],
            input="first\n",
            wait_seconds=10,
        )
        late = await _execute(
            provider,
            POLL_COMMAND,
            session_id=started["session_id"],
            input="second\n",
        )
        assert "not accepting input" in json.dumps(late)
        assert "tool execution failed" not in json.dumps(late)
        again = await _execute(provider, POLL_COMMAND, session_id=started["session_id"])
        assert "got:first" in again["stdout"]

    async def test_an_unknown_session_says_why(self, provider: WorkspaceToolProvider) -> None:
        payload = await _execute(provider, POLL_COMMAND, session_id="nope")
        assert "restart" in json.dumps(payload)


@_requires_sandbox
class TestBounds:
    async def test_output_is_capped_and_says_so(self, tmp_path: Path) -> None:
        registry = SessionRegistry()
        session_ws = WorkspaceSession(tmp_path)
        session = await registry.start(
            [
                "bash",
                "-lc",
                f"head -c {MAX_SESSION_OUTPUT_BYTES * 2} /dev/zero | tr '\\0' 'x'",
            ],
            policy=session_ws.policy,
            cwd=session_ws.root,
        )
        await session.wait_for(20)
        read = session.read()
        assert read.truncated["stdout"] is True
        assert len(read.stdout) <= MAX_SESSION_OUTPUT_BYTES
        await _close_registry(registry)

    async def test_closing_the_registry_ends_a_live_command(self, tmp_path: Path) -> None:
        session_ws = WorkspaceSession(tmp_path)
        registry = SessionRegistry()
        session = await registry.start(
            ["bash", "-lc", "sleep 60"],
            policy=session_ws.policy,
            cwd=session_ws.root,
        )
        assert session.running
        await _close_registry(registry)
        await asyncio.sleep(0.05)
        assert not session.running

    async def test_too_many_live_commands_is_refused(self, tmp_path: Path) -> None:
        session_ws = WorkspaceSession(tmp_path)
        registry = SessionRegistry()
        for _ in range(4):
            await registry.start(
                ["bash", "-lc", "sleep 60"],
                policy=session_ws.policy,
                cwd=session_ws.root,
            )
        with pytest.raises(RuntimeError, match="already running"):
            await registry.start(
                ["bash", "-lc", "sleep 60"],
                policy=session_ws.policy,
                cwd=session_ws.root,
            )
        await _close_registry(registry)


@_requires_sandbox
class TestAnInterruptedCommand:
    """Worker restarts lose sessions; the model needs explicit aborts, not
    unmatched calls."""

    async def test_the_resumed_transcript_says_the_call_was_aborted(self, tmp_path: Path) -> None:
        from co_scientist.platform.llm.tools.transcript import (
            ABORTED_RESULT,
            normalize_tool_transcript,
        )

        provider = WorkspaceToolProvider(WorkspaceSession(tmp_path))
        started = await _execute(
            provider,
            _WORKSPACE_SESSIONS_RUN_COMMAND,
            argv=["bash", "-lc", "sleep 60"],
            yield_seconds=0.05,
        )
        assert started["running"] is True

        interrupted = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_poll",
                        "type": "function",
                        "function": {
                            "name": POLL_COMMAND,
                            "arguments": json.dumps({"session_id": started["session_id"]}),
                        },
                    }
                ],
            }
        ]
        await _close_registry(provider.session.sessions)

        repaired = normalize_tool_transcript(interrupted)
        assert repaired[1]["tool_call_id"] == "call_poll"
        assert json.loads(repaired[1]["content"]) == ABORTED_RESULT
        payload = await _execute(provider, POLL_COMMAND, session_id=started["session_id"])
        assert "restart" in json.dumps(payload)


_SECRET = "sk-live-9f3c2b71aa4d8e60"


def _workspace_output_call(name: str, arguments: Any = "{}") -> SimpleNamespace:
    return make_tool_call(f"call_{name}", name, arguments)


def _registry(**secrets: str) -> SecretRegistry:
    registry = SecretRegistry()
    for name, value in secrets.items():
        registry.register(name, value)
    return registry


def _content(message: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(message["content"])
    return parsed


def _run(provider: WorkspaceToolProvider, argv: list[str]) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        try:
            return _content(
                await provider.execute_tool_call(
                    _workspace_output_call(
                        _WORKSPACE_OUTPUT_RUN_COMMAND,
                        json.dumps({"argv": argv}),
                    )
                )
            )
        finally:
            await _close_registry(provider.session.sessions)

    return asyncio.run(run())


def test_a_short_value_is_refused_rather_than_masked() -> None:
    with pytest.raises(SecretRegistrationError, match="cannot be masked"):
        _registry(TOKEN="a" * (MIN_SECRET_LENGTH - 1))


def test_environment_scan_finds_credential_shaped_names() -> None:
    registry = SecretRegistry()
    found = registry.register_environment(
        {
            "ANTHROPIC_API_KEY": _SECRET,
            "GITHUB_TOKEN": "ghp_0123456789abcdef",
            "PATH": "/usr/bin",
        }
    )
    assert set(found) == {"ANTHROPIC_API_KEY", "GITHUB_TOKEN"}
    assert registry.register_environment({"SHORT_KEY": "abc", "REAL_KEY": _SECRET}) == ("REAL_KEY",)


def test_a_secret_containing_another_is_masked_as_itself() -> None:
    registry = _registry(INNER=_SECRET, OUTER=f"{_SECRET}-extended-suffix")
    assert registry.redact(f"{_SECRET}-extended-suffix") == "[redacted:OUTER]"


@_requires_sandbox
def test_a_command_printing_a_secret_does_not_print_it(tmp_path: Path) -> None:
    """Command stdout enters the transcript directly and must redact injected
    credentials."""
    echo = shutil.which("echo")
    if echo is None:  # pragma: no cover - environment-dependent
        pytest.skip("echo is not installed")
    provider = WorkspaceToolProvider(WorkspaceSession(tmp_path), secrets=_registry(API_KEY=_SECRET))

    payload = _run(provider, [echo, _SECRET])

    assert _SECRET not in json.dumps(payload)
    assert "[redacted:API_KEY]" in payload["stdout"]


def test_reading_a_file_does_not_route_around_redaction(
    tmp_path: Path,
) -> None:
    (tmp_path / "captured.txt").write_text(f"key={_SECRET}\n")
    provider = WorkspaceToolProvider(WorkspaceSession(tmp_path), secrets=_registry(API_KEY=_SECRET))

    payload = _content(
        asyncio.run(
            provider.execute_tool_call(
                _workspace_output_call(READ_FILE, json.dumps({"path": "captured.txt"}))
            )
        )
    )

    assert _SECRET not in payload["content"]


def test_output_is_dropped_whole_when_a_value_survives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(SecretRegistry, "redact", lambda self, text: text)
    recorder = OutputRecorder(tmp_path, _registry(API_KEY=_SECRET))

    bounded = recorder.record("stdout", f"leaked {_SECRET}")

    assert _SECRET not in bounded.text
    assert "API_KEY" in bounded.text
    assert bounded.truncated


def test_a_long_stream_keeps_both_ends(tmp_path: Path) -> None:
    recorder = OutputRecorder(tmp_path, preview_chars=200)
    text = f"START{'x' * 5000}END"

    bounded = recorder.record("stdout", text)

    assert bounded.text.startswith("START")
    assert bounded.text.endswith("END")
    assert bounded.truncated


def test_spilled_output_is_redacted_before_it_is_written(
    tmp_path: Path,
) -> None:
    recorder = OutputRecorder(tmp_path, _registry(API_KEY=_SECRET), preview_chars=100)

    pointer = recorder.record("stdout", f"{_SECRET}{'x' * 5000}").pointer

    assert pointer is not None
    assert _SECRET not in (tmp_path / pointer.path).read_text()


def test_a_symlinked_spill_file_does_not_redirect_output(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    spill_directory = root / SPILL_DIRECTORY
    spill_directory.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    text = "x" * 5000
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    target = spill_directory / f"stdout-{digest[:12]}.txt"
    target.symlink_to(outside)

    bounded = OutputRecorder(root, preview_chars=100).record("stdout", text)

    assert bounded.pointer is None
    assert target.is_symlink()
    assert outside.read_text() == "keep"


def test_a_swapped_spill_parent_does_not_redirect_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    spill_directory = root / SPILL_DIRECTORY
    spill_directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    held_directory = spill_directory.with_name("output-held")
    swapped = False

    def swap_directory() -> None:
        nonlocal swapped
        if swapped:
            return
        spill_directory.rename(held_directory)
        spill_directory.symlink_to(outside, target_is_directory=True)
        swapped = True

    original_mkdir = Path.mkdir

    def mkdir_then_swap(path: Path, *args: Any, **kwargs: Any) -> None:
        original_mkdir(path, *args, **kwargs)
        if path == spill_directory:
            swap_directory()

    original_parent_fd = patch_module._parent_fd

    def pin_then_swap(root_fd: int, parts: tuple[str, ...], *, create: bool = False) -> int:
        fd = original_parent_fd(root_fd, parts, create=create)
        if parts[:-1] == tuple(SPILL_DIRECTORY.split("/")):
            swap_directory()
        return fd

    monkeypatch.setattr(Path, "mkdir", mkdir_then_swap)
    monkeypatch.setattr(patch_module, "_parent_fd", pin_then_swap)

    bounded = OutputRecorder(root, preview_chars=100).record("stdout", "x" * 5000)

    assert swapped
    assert list(outside.iterdir()) == []
    assert bounded.truncated


def test_a_failed_spill_costs_the_middle_not_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(workspace_output, "write_workspace_file", _explode)
    recorder = OutputRecorder(tmp_path, preview_chars=100)

    bounded = recorder.record("stdout", "x" * 5000)

    assert bounded.pointer is None
    assert bounded.truncated
    assert bounded.text


def test_the_spill_directory_is_harness_scratch_not_a_guarantee() -> None:
    """Landlock only adds access; protecting scratch would make every
    workspace inexpressible."""
    assert SPILL_DIRECTORY.split("/")[0] == HARNESS_METADATA_NAME
    assert HARNESS_METADATA_NAME not in PROTECTED_METADATA_NAMES
    assert HARNESS_METADATA_NAME in METADATA_NAMES


def test_listing_files_omits_harness_metadata(tmp_path: Path) -> None:
    session = WorkspaceSession(tmp_path)
    (tmp_path / "analysis.py").write_text("pass")
    OutputRecorder(session.root, preview_chars=50).record("stdout", "y" * 500)

    payload = _content(
        asyncio.run(
            WorkspaceToolProvider(session).execute_tool_call(_workspace_output_call(LIST_FILES))
        )
    )
    assert payload["files"] == ["analysis.py"]


def test_listing_files_omits_symlinks_to_existing_and_missing_targets(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "present.txt").write_text("outside")
    (root / "present-link.txt").symlink_to(outside / "present.txt")
    (root / "missing-link.txt").symlink_to(outside / "missing.txt")
    (root / "directory-link").symlink_to(outside, target_is_directory=True)
    (root / "ordinary.txt").write_text("inside")

    assert WorkspaceSession(root).list_files() == ("ordinary.txt",)


def test_listing_files_does_not_follow_a_directory_swapped_for_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    nested = root / "sub"
    nested.mkdir(parents=True)
    (nested / "secret.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    held = root / "held"
    swapped = False

    def swap_directory() -> None:
        nonlocal swapped
        if swapped:
            return
        nested.rename(held)
        nested.symlink_to(outside, target_is_directory=True)
        swapped = True

    original_lstat = Path.lstat

    def lstat_then_swap(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == nested / "secret.txt":
            swap_directory()
        return original_lstat(path, *args, **kwargs)

    original_open = os.open

    def open_then_swap(path: Any, *args: Any, **kwargs: Any) -> int:
        flags = args[0] if args else kwargs.get("flags", 0)
        if (
            path == "sub"
            and kwargs.get("dir_fd") is not None
            and flags & getattr(os, "O_DIRECTORY", 0)
        ):
            swap_directory()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat_then_swap)
    monkeypatch.setattr(os, "open", open_then_swap)

    assert WorkspaceSession(root).list_files() == ()
    assert swapped


def test_reopening_a_workspace_does_not_create_output_under_metadata_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    initial = WorkspaceSession(root)
    initial_spill = OutputRecorder(initial.root, preview_chars=100).record("stdout", "x" * 5000)
    assert initial_spill.pointer is not None
    shutil.rmtree(root / HARNESS_METADATA_NAME)
    (root / HARNESS_METADATA_NAME).symlink_to(outside, target_is_directory=True)

    reopened = WorkspaceSession(root)
    bounded = OutputRecorder(reopened.root, preview_chars=100).record("stdout", "x" * 5000)

    assert bounded.pointer is None
    assert not (outside / "output").exists()


def test_a_symlinked_metadata_directory_does_not_redirect_the_spill(
    tmp_path: Path,
) -> None:
    """Host-side spills must
    resist later symlinks."""
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / SPILL_DIRECTORY.split("/")[0]).symlink_to(outside)

    bounded = OutputRecorder(root, preview_chars=50).record("stdout", "x" * 5000)

    assert bounded.pointer is None
    # Even mkdir would let command-chosen paths cause a host-side write.
    assert list(outside.rglob("*")) == []


def test_a_truncated_read_hands_back_a_way_to_the_rest(
    tmp_path: Path,
) -> None:
    session = WorkspaceSession(tmp_path)
    (tmp_path / "big.txt").write_text("y" * 40_000)

    payload = _content(
        asyncio.run(
            WorkspaceToolProvider(session).execute_tool_call(
                _workspace_output_call(READ_FILE, json.dumps({"path": "big.txt"}))
            )
        )
    )

    assert payload["truncated"] is True
    assert payload["full_output"].startswith(SPILL_DIRECTORY)
    assert (tmp_path / payload["full_output"]).read_text() == "y" * 40_000
