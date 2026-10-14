from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from co_scientist.platform.llm import ToolLoop
from co_scientist.platform.llm.tool_effects import batch_by_effects
from co_scientist.platform.sandbox import (
    SandboxKind,
    SandboxPolicy,
    command_lifecycle_available,
    sandbox_backend,
    workspace_write,
)
from co_scientist.platform.sandbox.patch import PatchError
from co_scientist.platform.sandbox.workspace import (
    APPLY_PATCH,
    LIST_FILES,
    READ_FILE,
    RUN_COMMAND,
    SPILL_DIRECTORY,
    WORKSPACE_DIR_ENV,
    WRITE_FILE,
    OutputRecorder,
    SecretRegistry,
    WorkspaceIdError,
    WorkspaceSession,
    WorkspaceToolProvider,
    build_workspace_tools,
    can_run_commands,
    open_run_workspace,
    workspace_path,
    workspace_tool_schemas,
    workspaces_root,
)
from co_scientist.platform.sandbox.workspace import tools as workspace_tools
from co_scientist.platform.sandbox.workspace.checks import (
    MAX_CHECKED_BYTES,
    MAX_FINDINGS,
    check_paths,
)
from tests._llm_fake import make_tool_call

_requires_sandbox = pytest.mark.skipif(
    sandbox_backend() is None or not command_lifecycle_available(),
    reason="no filesystem and lifecycle sandbox on this platform",
)


def _session(tmp_path: Path) -> WorkspaceSession:
    return WorkspaceSession(tmp_path / "ws")


async def _run_command_tool(
    session: WorkspaceSession,
    argv: list[str],
    *,
    secret_registry: SecretRegistry | None = None,
    yield_seconds: float = 30,
) -> dict[str, Any]:
    provider = WorkspaceToolProvider(session, secrets=secret_registry)
    message = await provider.execute_tool_call(
        SimpleNamespace(
            id="call_run_command",
            function=SimpleNamespace(
                name=RUN_COMMAND,
                arguments=json.dumps({"argv": argv, "yield_seconds": yield_seconds}),
            ),
        )
    )
    return cast(dict[str, Any], json.loads(message["content"]))


@pytest.mark.asyncio
@_requires_sandbox
async def test_a_command_runs_inside_the_workspace(tmp_path: Path) -> None:
    session = _session(tmp_path)
    outcome = await _run_command_tool(session, ["/bin/pwd"])
    assert outcome["exit_code"] == 0, outcome["stderr"]
    assert outcome["stdout"].strip() == str(session.root)
    assert not outcome["required_approval"]
    written = await _run_command_tool(session, ["/usr/bin/touch", "made.txt"])
    assert written["exit_code"] == 0, written["stderr"]
    assert (session.root / "made.txt").exists()
    await session.close()


@pytest.mark.asyncio
@_requires_sandbox
async def test_an_unrecognized_command_is_flagged_for_approval(
    tmp_path: Path,
) -> None:
    """The flag records approval intent; sandbox confinement enforces access."""
    session = _session(tmp_path)
    flagged = await _run_command_tool(session, ["/usr/bin/tee", "out.txt"], yield_seconds=0.05)
    assert flagged["required_approval"]
    await session.close()


@pytest.mark.asyncio
@_requires_sandbox
async def test_a_command_cannot_write_outside_the_workspace(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    escape = tmp_path / "escaped.txt"
    outcome = await _run_command_tool(session, ["/usr/bin/touch", str(escape)])
    assert outcome["exit_code"] != 0
    assert not escape.exists()
    await session.close()


@pytest.mark.asyncio
@_requires_sandbox
async def test_the_host_environment_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    session = _session(tmp_path)
    outcome = await _run_command_tool(
        session,
        ["/usr/bin/env"],
        secret_registry=SecretRegistry(),
    )
    assert "sk-secret" not in outcome["stdout"]
    await session.close()


@pytest.mark.parametrize("path", ["../outside.txt", "sub/../../outside.txt"])
def test_reading_outside_the_workspace_is_refused(tmp_path: Path, path: str) -> None:
    session = _session(tmp_path)
    (tmp_path / "outside.txt").write_text("secret")
    with pytest.raises(PatchError, match="outside the workspace"):
        session.read_file(path)


@_requires_sandbox
@pytest.mark.asyncio
async def test_a_carve_out_backend_refuses_to_replace_the_metadata_dir(
    tmp_path: Path,
) -> None:
    """Landlock only adds access; seatbelt can protect writable carve-
    outs."""
    if sandbox_backend() == "landlock":
        pytest.skip("landlock cannot express a carve-out; see the next test")
    link = shutil.which("ln")
    if link is None:  # pragma: no cover - environment-dependent
        pytest.skip("ln is not installed")
    session = _session(tmp_path)
    metadata = SPILL_DIRECTORY.split("/")[0]

    outcome = await _run_command_tool(session, [link, "-sfn", "/tmp", metadata])

    assert outcome["exit_code"] != 0
    assert not (session.root / metadata).is_symlink()
    await session.close()


@_requires_sandbox
@pytest.mark.asyncio
async def test_replacing_the_metadata_dir_cannot_redirect_a_host_write(
    tmp_path: Path,
) -> None:
    """Host writes must reject redirected targets even without backend carve-
    outs."""
    link = shutil.which("ln")
    if link is None:  # pragma: no cover - environment-dependent
        pytest.skip("ln is not installed")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    session = _session(tmp_path)
    metadata = SPILL_DIRECTORY.split("/")[0]

    await _run_command_tool(session, [link, "-sfn", str(outside), metadata])
    OutputRecorder(session.root, preview_chars=50).record("stdout", "x" * 900)

    assert list(outside.rglob("*")) == []
    await session.close()


def _apply(tmp_path: Path, body: list[str]) -> dict[str, Any]:
    patch = "\n".join(["*** Begin Patch", *body, "*** End Patch", ""])
    provider = WorkspaceToolProvider(WorkspaceSession(tmp_path))
    message = asyncio.run(
        provider.execute_tool_call(
            SimpleNamespace(
                id="call_apply_patch",
                function=SimpleNamespace(
                    name="apply_patch",
                    arguments=json.dumps({"patch": patch}),
                ),
            )
        )
    )
    parsed: dict[str, Any] = json.loads(message["content"])
    return parsed


def test_findings_are_capped(tmp_path: Path) -> None:
    names = []
    for index in range(MAX_FINDINGS + 5):
        name = f"broken_{index}.py"
        (tmp_path / name).write_text("def f(:")
        names.append(name)
    assert len(check_paths(tmp_path, names)) == MAX_FINDINGS


@pytest.fixture
def _isolated_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(WORKSPACE_DIR_ENV, str(tmp_path / "workspaces"))


@pytest.mark.usefixtures("_isolated_root")
class TestRunWorkspace:
    def test_reopening_a_run_returns_the_same_directory(self) -> None:
        first = open_run_workspace("run-abc")
        (first.root / "partial.csv").write_text("1,2\n")

        second = open_run_workspace("run-abc")

        assert second.root == first.root
        assert (second.root / "partial.csv").read_text() == "1,2\n"

    def test_two_runs_do_not_share_a_workspace(self) -> None:
        assert open_run_workspace("run-a").root != open_run_workspace("run-b").root

    def test_a_traversing_run_id_stays_under_the_root(self) -> None:
        resolved = workspace_path("../../etc/shadow")
        assert workspaces_root() in resolved.parents

    def test_a_run_id_with_nothing_usable_is_refused(self) -> None:
        with pytest.raises(WorkspaceIdError):
            workspace_path("../..")

    def test_the_workspace_is_the_only_writable_root(self) -> None:
        session = open_run_workspace("run-policy")
        assert session.policy.writable_roots == (session.root,)

    def test_network_is_off_unless_asked_for(self) -> None:
        assert not open_run_workspace("run-net").policy.allows_network
        assert open_run_workspace("run-net-on", network_allowed=True).policy.allows_network

    def test_the_tool_provider_is_bound_to_the_run(self) -> None:
        provider = build_workspace_tools("run-tools")
        assert provider.session.root == workspace_path("run-tools")


def _call(name: str, arguments: Any = "{}") -> SimpleNamespace:
    return make_tool_call(f"call_{name}", name, arguments)


def _provider(tmp_path: Path, **kwargs: Any) -> WorkspaceToolProvider:
    return WorkspaceToolProvider(WorkspaceSession(tmp_path), **kwargs)


def _content(message: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(message["content"])
    return parsed


def _names(schemas: list[dict[str, Any]]) -> list[str]:
    return [schema["function"]["name"] for schema in schemas]


def test_command_tool_is_withheld_without_a_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Offering an unusable tool spends model turns on an unrecoverable
    error."""
    monkeypatch.setattr(workspace_tools, "sandbox_backend", lambda: None)
    schemas = workspace_tool_schemas(workspace_write(tmp_path))
    assert RUN_COMMAND not in _names(schemas)
    assert READ_FILE in _names(schemas)


def test_command_tool_is_withheld_without_lifecycle_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(workspace_tools, "sandbox_backend", lambda: "landlock")
    monkeypatch.setattr(workspace_tools, "command_lifecycle_available", lambda: False)
    schemas = workspace_tool_schemas(workspace_write(tmp_path))
    assert RUN_COMMAND not in _names(schemas)
    if sys.platform.startswith("linux"):
        assert "no writable delegated cgroup v2 lifecycle boundary" in caplog.text
    else:
        assert "command lifecycle isolation is unavailable for this platform" in caplog.text
    assert "no sandbox backend on this platform" not in caplog.text


def test_workspace_tool_schema_checks_capability_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = 0

    def unavailable_reason(policy: SandboxPolicy | None = None) -> str:
        nonlocal calls
        calls += 1
        return "no writable delegated cgroup v2 lifecycle boundary"

    monkeypatch.setattr(workspace_tools, "command_execution_unavailable_reason", unavailable_reason)

    schemas = workspace_tool_schemas(workspace_write(tmp_path))

    assert RUN_COMMAND not in _names(schemas)
    assert calls == 1


def test_external_confinement_keeps_the_command_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_tools, "sandbox_backend", lambda: None)
    assert can_run_commands(SandboxPolicy(kind=SandboxKind.EXTERNAL))
    assert can_run_commands(SandboxPolicy(kind=SandboxKind.DANGER_FULL_ACCESS))


@pytest.mark.parametrize(
    "backend,protected,offered",
    [("landlock", True, False), ("landlock", False, True), ("seatbelt", True, True)],
)
def test_command_tools_require_backend_support_for_protected_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: str,
    protected: bool,
    offered: bool,
) -> None:
    if protected:
        (tmp_path / ".git").mkdir()
    monkeypatch.setattr(workspace_tools, "sandbox_backend", lambda: backend)
    monkeypatch.setattr(workspace_tools, "command_lifecycle_available", lambda: True)
    schemas = workspace_tool_schemas(workspace_write(tmp_path))
    assert (RUN_COMMAND in _names(schemas)) is offered
    assert READ_FILE in _names(schemas)


class _StubDelegate:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def execute_tool_call(self, tool_call: Any) -> dict[str, Any]:
        self.seen.append(tool_call.function.name)
        return {"role": "tool", "name": tool_call.function.name}


def test_non_workspace_calls_reach_the_delegate(tmp_path: Path) -> None:
    delegate = _StubDelegate()
    provider = _provider(tmp_path, delegate=delegate)
    asyncio.run(provider.execute_tool_call(_call("search_pubmed")))
    assert delegate.seen == ["search_pubmed"]


def test_a_shadowed_mcp_tool_is_dropped_not_duplicated(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path)
    merged = provider.merge_tools(
        [
            {"type": "function", "function": {"name": READ_FILE}},
            {"type": "function", "function": {"name": "search_pubmed"}},
        ]
    )
    assert _names(merged).count(READ_FILE) == 1
    assert "search_pubmed" in _names(merged)


def _loop(names: list[str]) -> ToolLoop:

    async def executor(tool_call: Any) -> dict[str, Any]:
        return {"role": "tool"}

    return ToolLoop(
        tools=[{"type": "function", "function": {"name": name}} for name in names],
        executor=executor,
    )


@pytest.mark.parametrize(
    ("name", "content", "problem"),
    [
        ("broken.py", "def f(:\n    pass", "SyntaxError"),
        ("config.json", '{"a": ,}', "invalid JSON"),
        ("config.yaml", "a: [1, 2\nb: 3", "invalid YAML"),
        ("fine.py", "VALUE = 1", None),
        ("notes.md", "# heading (", None),
    ],
)
def test_an_edit_comes_back_with_its_syntax_findings(
    tmp_path: Path, name: str, content: str, problem: str | None
) -> None:
    """A parsing veto would prevent writing a program in multiple steps, so
    the finding rides along with a write that still happens."""
    body = [f"+{line}" for line in content.split("\n")]
    payload = _apply(tmp_path, [f"*** Add File: {name}", *body])
    assert payload["changed"] == [name]
    assert (tmp_path / name).exists()
    if problem is None:
        assert "problems" not in payload
    else:
        assert problem in payload["problems"][0]["message"]


def test_a_binary_or_deleted_path_is_not_a_finding(tmp_path: Path) -> None:
    (tmp_path / "blob.json").write_bytes(b"\xff\xfe\x00binary")
    assert check_paths(tmp_path, ["gone.py", "blob.json"]) == ()


def test_a_checker_does_not_read_a_path_swapped_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "module.py"
    target.write_text("VALUE = 1\n")
    outside = tmp_path / "outside.py"
    outside.write_text("def broken(:\n")
    original_stat: Callable[..., os.stat_result] = os.stat
    swapped = False

    def stat_then_swap(
        path: os.PathLike[str] | os.PathLike[bytes] | str | bytes | int,
        *args: Any,
        **kwargs: Any,
    ) -> os.stat_result:
        nonlocal swapped
        result = original_stat(path, *args, **kwargs)
        is_path_based_check = path == target and kwargs.get("dir_fd") is None
        is_descriptor_check = path == "module.py" and kwargs.get("dir_fd") is not None
        if not swapped and (is_path_based_check or is_descriptor_check):
            target.unlink()
            target.symlink_to(outside)
            swapped = True
        return result

    monkeypatch.setattr(os, "stat", stat_then_swap)

    assert check_paths(root, ["module.py"]) == ()
    assert swapped


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes require POSIX")
def test_a_checker_does_not_block_on_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "blocked.py"
    os.mkfifo(fifo)
    findings: list[Any] = []

    def inspect() -> None:
        try:
            findings.append(check_paths(tmp_path, [fifo.name]))
        except BaseException as exc:
            findings.append(exc)

    worker = threading.Thread(target=inspect, daemon=True)
    worker.start()
    worker.join(timeout=0.25)
    completed_without_writer = not worker.is_alive()

    if not completed_without_writer:

        def release_blocked_open() -> None:
            fd = os.open(fifo, os.O_WRONLY)
            os.close(fd)

        writer = threading.Thread(target=release_blocked_open, daemon=True)
        writer.start()
        worker.join(timeout=1)
        writer.join(timeout=1)

    assert completed_without_writer
    assert findings == [()]


def test_a_checker_skips_a_file_that_grows_over_the_limit_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "large.py"
    target.write_text("def broken(:\n")
    original_stat: Callable[..., os.stat_result] = os.stat
    grew = False

    def stat_then_grow(
        path: os.PathLike[str] | os.PathLike[bytes] | str | bytes | int,
        *args: Any,
        **kwargs: Any,
    ) -> os.stat_result:
        nonlocal grew
        result = original_stat(path, *args, **kwargs)
        is_path_based_check = path == target and kwargs.get("dir_fd") is None
        is_descriptor_check = path == "large.py" and kwargs.get("dir_fd") is not None
        if not grew and (is_path_based_check or is_descriptor_check):
            target.write_text("def broken(:\n" + " " * MAX_CHECKED_BYTES)
            grew = True
        return result

    monkeypatch.setattr(os, "stat", stat_then_grow)

    assert check_paths(root, ["large.py"]) == ()
    assert grew


@pytest.mark.parametrize(
    ("tool", "arguments", "error"),
    [
        (READ_FILE, {"path": "../outside.txt"}, "outside"),
        (RUN_COMMAND, {"argv": "ls -la | wc -l"}, "bash"),
        (WRITE_FILE, {"path": "model.py"}, "content must be a string"),
        (WRITE_FILE, {"path": "../x.py", "content": "no"}, "outside"),
        ("search_pubmed", {}, "unknown tool"),
        (LIST_FILES, "{not json", "JSON"),
    ],
)
async def test_a_bad_call_is_answered_not_raised(
    tmp_path: Path,
    tool: str,
    arguments: Any,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Providers reject a transcript containing unanswered tool calls."""
    monkeypatch.setattr(workspace_tools, "sandbox_backend", lambda: "landlock")
    monkeypatch.setattr(workspace_tools, "command_lifecycle_available", lambda: True)
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    result = await _provider(tmp_path).execute_tool_call(_call(tool, raw))
    assert result["tool_call_id"] == f"call_{tool}"
    assert error in _content(result)["error"]
    assert not (tmp_path.parent / "x.py").exists()


async def test_the_file_tools_round_trip_through_tool_calls(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path)

    async def call(name: str, **arguments: Any) -> dict[str, Any]:
        return _content(await provider.execute_tool_call(_call(name, json.dumps(arguments))))

    written = await call(WRITE_FILE, path="sub/model.py", content="print(1)\n")
    assert written["bytes"] == len("print(1)\n")
    await call(WRITE_FILE, path="sub/model.py", content="print(2)\n")
    patch = "*** Begin Patch\n*** Add File: hello.py\n+hi\n*** End Patch\n"
    assert (await call(APPLY_PATCH, patch=patch))["changed"] == ["hello.py"]

    assert (await call(READ_FILE, path="sub/model.py"))["content"] == ("print(2)\n")
    assert (await call(LIST_FILES))["files"] == ["hello.py", "sub/model.py"]
    # Sibling reads must not observe a half-written workspace tree.
    batches = batch_by_effects([_call(READ_FILE), _call(WRITE_FILE), _call(APPLY_PATCH)])
    assert [len(batch) for batch in batches] == [1, 1, 1]
