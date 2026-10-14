from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from co_scientist.platform.sandbox import workspace_write
from co_scientist.platform.sandbox.runner import _terminate as terminate
from co_scientist.platform.sandbox.workspace import WorkspaceSession, WorkspaceToolProvider
from co_scientist.platform.sandbox.workspace import session as sessions
from co_scientist.science.generation.literature_tools import draft
from tests._state import make_state


@pytest.fixture
def local_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def spawn(argv: list[str], *_: Any, **__: Any) -> tuple[Any, None]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return proc, None

    monkeypatch.setattr(sessions, "create_command_process", spawn)


async def _start(registry: sessions.SessionRegistry, root: Path) -> sessions.CommandSession:
    return await registry.start(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        policy=workspace_write(root),
        cwd=root,
    )


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "parse_failure"])
async def test_draft_reaps_persistent_commands_on_every_exit(
    local_processes: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    workspace = WorkspaceToolProvider(WorkspaceSession(tmp_path))
    command = await _start(workspace.session.sessions, tmp_path)
    skills = draft.DraftSkills(workspace, [])

    async def prepare(*_: Any) -> tuple[draft.DraftSkills, int, str]:
        return skills, 1, "prompt"

    async def invoke(*_: Any) -> tuple[str, int]:
        if outcome == "failure":
            raise RuntimeError("draft failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return ('{"drafts": []}' if outcome == "success" else "invalid JSON"), 1

    monkeypatch.setattr(draft, "_prepare_draft_call", prepare)
    monkeypatch.setattr(draft, "_invoke_draft_llm", invoke)
    monkeypatch.setattr(workspace, "tracked_executor", lambda _: (None, {}))
    try:
        if outcome == "success":
            assert await draft.draft_hypotheses(make_state(), 1, None) == ([], 1)
        else:
            with pytest.raises((Exception, asyncio.CancelledError)):
                await draft.draft_hypotheses(make_state(), 1, None)
        assert not command.running
        assert not workspace.session.sessions._sessions
    finally:
        await command.close()


async def test_draft_preparation_failure_also_closes_workspace(
    local_processes: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = WorkspaceToolProvider(WorkspaceSession(tmp_path))
    command = await _start(workspace.session.sessions, tmp_path)
    monkeypatch.setattr(draft, "_setup_tool_provider", lambda *_args, **_kwargs: (None, [], None))
    monkeypatch.setattr(draft, "attach_skills", lambda *_args: draft.DraftSkills(workspace))

    def fail(*_: Any) -> Any:
        raise ValueError("prompt failed")

    monkeypatch.setattr(draft, "_invoke_draft_prompt_builder", fail)
    try:
        with pytest.raises(ValueError, match="prompt failed"):
            await draft._prepare_draft_call(make_state(), 1, None, None, None)
        assert not command.running
    finally:
        await command.close()


async def test_sessions_expire_without_workspace_cleanup(
    local_processes: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "MAX_SESSION_LIFETIME_SECONDS", 0.05)
    registry = sessions.SessionRegistry()
    command = await _start(registry, tmp_path)
    try:
        assert command._lifecycle_task is not None
        await asyncio.wait_for(command._lifecycle_task, 5)
        assert not command.running
    finally:
        await registry.close()


async def test_process_wide_admission_is_shared_and_released(
    local_processes: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "_process_sessions", threading.BoundedSemaphore(2))
    registries = [sessions.SessionRegistry() for _ in range(3)]
    try:
        await _start(registries[0], tmp_path)
        await _start(registries[1], tmp_path)
        with pytest.raises(RuntimeError, match="process-wide"):
            await _start(registries[2], tmp_path)
        await registries[0].close()
        await _start(registries[2], tmp_path)
    finally:
        for registry in registries:
            await registry.close()


async def test_concurrent_starts_cannot_race_the_workspace_limit(
    local_processes: None, tmp_path: Path
) -> None:
    registry = sessions.SessionRegistry()
    try:
        results = await asyncio.gather(
            *[_start(registry, tmp_path) for _ in range(10)], return_exceptions=True
        )
        assert (
            sum(isinstance(result, sessions.CommandSession) for result in results)
            == sessions.MAX_LIVE_SESSIONS
        )
    finally:
        await registry.close()


async def test_repeated_cancellation_cannot_interrupt_workspace_reaping(
    local_processes: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = sessions.SessionRegistry()
    commands = [await _start(registry, tmp_path) for _ in range(2)]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_terminate(*args: Any) -> None:
        entered.set()
        await release.wait()
        await terminate(*args)

    monkeypatch.setattr(sessions, "_terminate", delayed_terminate)
    closing = asyncio.create_task(registry.close())
    try:
        await entered.wait()
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await registry.close()
    assert not registry._sessions
    assert all(not command.running for command in commands)
