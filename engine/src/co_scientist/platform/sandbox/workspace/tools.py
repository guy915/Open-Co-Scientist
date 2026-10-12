"""Withhold unconfineable execution tools rather than wasting model turns.
Local command deadlines remain separate from MCP transport timeouts.
"""

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from co_scientist.platform.llm.tool_effects import declare_local_tool
from co_scientist.platform.llm.tools.messages import tool_error_message, tool_result_message
from co_scientist.platform.llm.tools.messages import tracked_executor as track_calls
from co_scientist.platform.sandbox import (
    SandboxKind,
    SandboxPolicy,
    command_lifecycle_available,
    is_known_safe,
    landlock,
    sandbox_backend,
)
from co_scientist.platform.sandbox.patch import PatchError
from co_scientist.platform.sandbox.skills import (
    available_skills,
    invoked_skill,
    read_skill_document,
    record_skill_use,
    skill_environment,
)
from co_scientist.platform.sandbox.workspace.checks import check_paths
from co_scientist.platform.sandbox.workspace.output import (
    BoundedOutput,
    OutputRecorder,
    SecretRegistry,
)
from co_scientist.platform.sandbox.workspace.session import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    CommandSession,
    SessionRead,
    WorkspaceSession,
)
from co_scientist.platform.sandbox.workspace.tool_schemas import (
    APPLY_PATCH,
    DEFAULT_YIELD_SECONDS,
    LIST_FILES,
    POLL_COMMAND,
    READ_FILE,
    READ_SKILL,
    RUN_COMMAND,
    WRITE_FILE,
    apply_patch_schema,
    list_files_schema,
    poll_command_schema,
    read_file_schema,
    read_skill_schema,
    run_command_schema,
    write_file_schema,
)

logger = logging.getLogger(__name__)


class WorkspaceToolInputError(ValueError):
    """Bad model arguments must return an answered tool call, not terminate the
    conversation.
    """


@dataclass(frozen=True)
class _ToolContext:
    session: WorkspaceSession
    recorder: OutputRecorder


async def _handle_apply_patch(context: "_ToolContext", args: dict[str, Any]) -> dict[str, Any]:
    session = context.session
    patch_text = args.get("patch")
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise WorkspaceToolInputError("patch must be a non-empty string")
    # Offload synchronous multi-file parsing/writing rather than blocking a
    # cohort's event loop.
    outcome = await asyncio.to_thread(session.apply_patch_text, patch_text)
    payload: dict[str, Any] = {
        "changed": list(outcome.changed),
        "match_rungs": {path: list(rungs) for path, rungs in outcome.rungs.items()},
    }
    findings = await asyncio.to_thread(check_paths, session.root, outcome.changed)
    if findings:
        payload["problems"] = [finding.as_dict() for finding in findings]
    return payload


async def _handle_read_file(context: "_ToolContext", args: dict[str, Any]) -> dict[str, Any]:
    """Files can contain command output; redaction here prevents bypassing
    stdout protection.
    """
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        raise WorkspaceToolInputError("path must be a non-empty string")
    content = await asyncio.to_thread(context.session.read_file, path)
    bounded = context.recorder.record("read_file", content)
    payload: dict[str, Any] = {
        "path": path,
        "content": bounded.text,
        "truncated": bounded.truncated,
    }
    if bounded.pointer is not None:
        # Expose the spill path so re-reading can reach the middle instead of
        # repeating the preview.
        payload["full_output"] = bounded.pointer.path
    return payload


async def _handle_write_file(context: "_ToolContext", args: dict[str, Any]) -> dict[str, Any]:
    """Whole-file writes must run the same safety scan as patches to avoid
    bypassing it.
    """
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        raise WorkspaceToolInputError("path must be a non-empty string")
    content = args.get("content")
    if not isinstance(content, str):
        raise WorkspaceToolInputError("content must be a string")
    await asyncio.to_thread(context.session.write_file, path, content)
    payload: dict[str, Any] = {"path": path, "bytes": len(content.encode())}
    findings = await asyncio.to_thread(check_paths, context.session.root, [path])
    if findings:
        payload["problems"] = [finding.as_dict() for finding in findings]
    return payload


async def _handle_list_files(context: "_ToolContext", args: dict[str, Any]) -> dict[str, Any]:
    del args
    files = await asyncio.to_thread(context.session.list_files)
    return {"files": list(files)}


def _require_argv(args: dict[str, Any]) -> list[str]:
    argv = args.get("argv")
    if isinstance(argv, str):
        raise WorkspaceToolInputError(
            "argv must be an array of strings, not a single string; pass "
            '["bash", "-lc", "<command>"] to run something through a shell'
        )
    if not isinstance(argv, list) or not argv:
        raise WorkspaceToolInputError("argv must be a non-empty array")
    if not all(isinstance(item, str) for item in argv):
        raise WorkspaceToolInputError("every argv entry must be a string")
    return argv


def _resolve_seconds(raw: Any, default: float, floor: float = 0.0) -> float:
    if not isinstance(raw, int | float) or raw < floor:
        return default
    return min(float(raw), DEFAULT_COMMAND_TIMEOUT_SECONDS)


def _full_output_paths(
    streams: dict[str, BoundedOutput],
) -> dict[str, str]:
    return {
        name: bounded.pointer.path
        for name, bounded in streams.items()
        if bounded.pointer is not None
    }


def _session_payload(context: "_ToolContext", read: SessionRead) -> dict[str, Any]:
    """A running command is a successful session response; poll it rather than
    restarting.
    """
    streams = {
        "stdout": context.recorder.record("stdout", read.stdout),
        "stderr": context.recorder.record("stderr", read.stderr),
    }
    payload: dict[str, Any] = {
        "session_id": read.session_id,
        "running": read.running,
        "exit_code": read.exit_code,
        "stdout": streams["stdout"].text,
        "stderr": streams["stderr"].text,
        "cursor": read.cursor,
        "truncated": {
            name: bounded.truncated or read.truncated[name] for name, bounded in streams.items()
        },
    }
    spilled = _full_output_paths(streams)
    if spilled:
        payload["full_output"] = spilled
    return payload


async def _handle_run_command(context: "_ToolContext", args: dict[str, Any]) -> dict[str, Any]:
    """Sessions preserve long-running work beyond the initial wait instead of
    killing it.
    """
    argv = _require_argv(args)
    # Inject credentials only into recognized skill scripts, never arbitrary
    # network-capable model programs.
    skill = (
        invoked_skill(
            argv,
            cwd=context.session.root,
            writable_roots=context.session.policy.writable_roots,
        )
        if context.session.skills_enabled
        else None
    )
    if skill is not None:
        # Attribution is per data source; run_command alone cannot identify the
        # notice owed.
        record_skill_use(skill)
    env_extra = skill_environment(skill) if skill is not None else None
    session = await context.session.sessions.start(
        argv,
        policy=context.session.policy,
        cwd=context.session.root,
        env_extra=env_extra,
    )
    await session.wait_for(
        _resolve_seconds(args.get("yield_seconds"), DEFAULT_YIELD_SECONDS, floor=0.001)
    )
    payload = _session_payload(context, session.read())
    payload["required_approval"] = not is_known_safe(argv)
    return payload


def _named_session(context: "_ToolContext", args: dict[str, Any]) -> CommandSession:
    session_id = args.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise WorkspaceToolInputError("session_id must be a string")
    try:
        return context.session.sessions.get(session_id)
    except KeyError:
        raise WorkspaceToolInputError(
            f"no command session {session_id!r} is open here. A worker "
            f"restart ends every session it was running."
        ) from None


async def _send_input(session: CommandSession, args: dict[str, Any]) -> None:
    if not isinstance(args.get("input"), str):
        return
    try:
        await session.write(str(args["input"]))
    except ValueError as exc:
        # An exited command cannot accept input; polling still retrieves its
        # output.
        raise WorkspaceToolInputError(str(exc)) from None


async def _handle_poll_command(context: "_ToolContext", args: dict[str, Any]) -> dict[str, Any]:
    session = _named_session(context, args)
    await _send_input(session, args)
    if args.get("kill"):
        await session.close()
    else:
        await session.wait_for(_resolve_seconds(args.get("wait_seconds"), DEFAULT_YIELD_SECONDS))
    cursor = args.get("cursor") if isinstance(args.get("cursor"), dict) else {}
    return _session_payload(context, session.read(cursor))


# Declare effects before exposure: writes/processes are barriers, reads may
# batch.
declare_local_tool(RUN_COMMAND, WorkspaceSession.RUN_COMMAND_EFFECTS)
declare_local_tool(APPLY_PATCH, WorkspaceSession.APPLY_PATCH_EFFECTS)
declare_local_tool(READ_FILE, WorkspaceSession.READ_FILE_EFFECTS)
declare_local_tool(WRITE_FILE, WorkspaceSession.WRITE_FILE_EFFECTS)
declare_local_tool(LIST_FILES, WorkspaceSession.READ_FILE_EFFECTS)
# Polling can write stdin or terminate a live process, so it is a barrier.
declare_local_tool(POLL_COMMAND, WorkspaceSession.RUN_COMMAND_EFFECTS)
# Bundled skill instructions are immutable reads and can batch with other reads.
declare_local_tool(READ_SKILL, WorkspaceSession.READ_FILE_EFFECTS)

# Explicit external/declined confinement legitimately needs no local backend.
_BACKEND_EXEMPT_KINDS = (SandboxKind.EXTERNAL, SandboxKind.DANGER_FULL_ACCESS)


def command_execution_unavailable_reason(policy: SandboxPolicy | None = None) -> str | None:
    if policy is not None and policy.kind in _BACKEND_EXEMPT_KINDS:
        return None
    backend = sandbox_backend()
    if backend is None:
        return (
            "Landlock ABI 3 or newer is unavailable"
            if sys.platform.startswith("linux")
            else f"no supported command sandbox backend for {sys.platform}"
        )
    if policy is not None and backend == "landlock" and not landlock.can_enforce(policy):
        return "Landlock cannot preserve protected metadata in this workspace"
    if not command_lifecycle_available():
        if sys.platform.startswith("linux"):
            return (
                "no writable delegated cgroup v2 lifecycle boundary; configure "
                "COSCIENTIST_CGROUP_ROOT to a delegated subtree"
            )
        return "command lifecycle isolation is unavailable for this platform"
    return None


def can_run_commands(policy: SandboxPolicy) -> bool:
    return command_execution_unavailable_reason(policy) is None


def workspace_tool_schemas(
    policy: SandboxPolicy, *, skills_enabled: bool = False
) -> list[dict[str, Any]]:
    """Installing skills must not enable consumers that were measured worse
    with them.
    """
    schemas = [
        write_file_schema(),
        apply_patch_schema(),
        read_file_schema(),
        list_files_schema(),
    ]
    unavailable = command_execution_unavailable_reason(policy)
    # Offer skill instructions only when the consumer asks and commands can
    # actually run.
    skills = available_skills() if skills_enabled and unavailable is None else ()
    if skills:
        schemas.append(read_skill_schema(tuple(s.name for s in skills)))
    if unavailable is None:
        schemas.insert(0, run_command_schema(network_allowed=policy.allows_network))
        schemas.insert(1, poll_command_schema())
    else:
        logger.warning(
            "command execution unavailable (%s); withholding %r from the model",
            unavailable,
            RUN_COMMAND,
        )
    return schemas


async def _handle_read_skill(context: "_ToolContext", args: dict[str, Any]) -> dict[str, Any]:
    """Bundled instructions contain no command-injected secrets; do not
    truncate or redact them.
    """
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorkspaceToolInputError("name must be a non-empty string")
    path = args.get("path")
    if path is not None and not isinstance(path, str):
        raise WorkspaceToolInputError("path must be a string")
    document = await asyncio.to_thread(
        read_skill_document,
        name,
        path,
        network_allowed=context.session.policy.allows_network,
    )
    if document is None:
        if path:
            raise WorkspaceToolInputError(
                f"skill {name!r} has no file {path!r}; the path is relative "
                "to the skill directory, as its own document writes it"
            )
        available = ", ".join(skill.name for skill in available_skills())
        raise WorkspaceToolInputError(f"no skill named {name!r}; available skills: {available}")
    return {"name": name, "path": path or "SKILL.md", "instructions": document}


_HANDLERS = {
    RUN_COMMAND: _handle_run_command,
    POLL_COMMAND: _handle_poll_command,
    APPLY_PATCH: _handle_apply_patch,
    READ_FILE: _handle_read_file,
    WRITE_FILE: _handle_write_file,
    LIST_FILES: _handle_list_files,
    READ_SKILL: _handle_read_skill,
}


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise WorkspaceToolInputError(f"arguments were not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise WorkspaceToolInputError("arguments must be a JSON object")
    return parsed


class WorkspaceToolProvider:
    def __init__(
        self,
        session: WorkspaceSession,
        delegate: Any | None = None,
        secrets: SecretRegistry | None = None,
    ) -> None:
        self.session = session
        self._delegate = delegate
        if secrets is None:
            secrets = SecretRegistry()
            secrets.register_environment()
        self._context = _ToolContext(
            session=session,
            recorder=OutputRecorder(session.root, secrets),
        )
        self._tool_schemas = workspace_tool_schemas(
            session.policy, skills_enabled=session.skills_enabled
        )
        self._names = {schema["function"]["name"] for schema in self._tool_schemas}

    def _schemas(self) -> list[dict[str, Any]]:
        return list(self._tool_schemas)

    def tracked_executor(
        self, label: str
    ) -> tuple[Callable[[Any], Awaitable[dict[str, Any]]], dict[str, int]]:
        return track_calls(self, label)

    def get_tools(self) -> tuple[set[str], list[dict[str, Any]]]:
        return set(self._names), self._schemas()

    def merge_tools(self, mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = []
        for schema in mcp_tools:
            name = schema.get("function", {}).get("name")
            if name in self._names:
                logger.warning(
                    "MCP tool %r is shadowed by the workspace tool of the "
                    "same name and will not be offered",
                    name,
                )
                continue
            kept.append(schema)
        return [*self._schemas(), *kept]

    async def execute_tool_call(self, tool_call: Any) -> dict[str, Any]:
        """Every call needs a tool-role answer; raising leaves an unanswered
        call the next turn rejects.
        """
        name = tool_call.function.name
        handler = _HANDLERS.get(name) if name in self._names else None
        if handler is None:
            return await self._delegate_call(tool_call, name)

        try:
            payload = await handler(self._context, _parse_arguments(tool_call.function.arguments))
        except (WorkspaceToolInputError, PatchError) as exc:
            logger.info("workspace tool %s rejected a call: %s", name, exc)
            return tool_error_message(name, tool_call.id, str(exc))
        except Exception as exc:
            logger.exception("workspace tool %s failed", name)
            return tool_error_message(name, tool_call.id, f"tool execution failed: {exc}")
        return tool_result_message(name, tool_call.id, payload)

    async def _delegate_call(self, tool_call: Any, name: str) -> dict[str, Any]:
        if self._delegate is None:
            return tool_error_message(name, tool_call.id, f"unknown tool: {name}")
        result: dict[str, Any] = await self._delegate.execute_tool_call(tool_call)
        return result
