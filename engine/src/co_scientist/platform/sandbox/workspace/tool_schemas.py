"""Assigned descriptions are the model's tool interface; preserve their
constraints.
"""

from typing import Any

from co_scientist.platform.sandbox.workspace.session import DEFAULT_COMMAND_TIMEOUT_SECONDS

RUN_COMMAND = "run_command"
APPLY_PATCH = "apply_patch"
READ_FILE = "read_file"
WRITE_FILE = "write_file"
LIST_FILES = "list_files"
POLL_COMMAND = "poll_command"
READ_SKILL = "read_skill"

# Yield quickly without discarding work; the next poll can observe progress.
DEFAULT_YIELD_SECONDS = 10.0

_YIELD_SECONDS = {
    "type": "number",
    "description": (
        "How long to wait for the command before returning a session to "
        f"poll instead. Capped at {int(DEFAULT_COMMAND_TIMEOUT_SECONDS)} "
        f"seconds; defaults to {int(DEFAULT_YIELD_SECONDS)}."
    ),
}


def run_command_schema(*, network_allowed: bool = False) -> dict[str, Any]:
    """Network descriptions must match actual policy or models will refuse
    usable database skills.
    """
    reach = (
        "reach the network only through the commands you are asked to run"
        if network_allowed
        else "not reach the network"
    )
    return {
        "type": "function",
        "function": {
            "name": RUN_COMMAND,
            "description": (
                "Run a command inside the run's isolated workspace. The "
                "command is confined by the operating system: it can read "
                "the filesystem, write only inside the workspace, and "
                f"can {reach}. No shell is involved unless you "
                "ask for one -- pass "
                '["bash", "-lc", "..."] to use pipes, redirection or '
                "environment assignment. Output is captured and truncated "
                "if very large. A command that has not finished within "
                "yield_seconds is NOT killed and is NOT an error: the "
                "reply comes back with running=true and a session_id, "
                "and you continue it with poll_command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "The command and its arguments, already split. "
                            'e.g. ["python3", "analyze.py", "--fast"].'
                        ),
                    },
                    "yield_seconds": _YIELD_SECONDS,
                },
                "required": ["argv"],
            },
        },
    }


def apply_patch_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": APPLY_PATCH,
            "description": (
                "Create, modify, delete or move files in the workspace "
                "using a context-anchored patch. Hunks are located by the "
                "surrounding lines you quote, not by line number, so quote "
                "enough context to be unambiguous. The whole patch applies "
                "or nothing does: if any hunk's context does not match, "
                "no file is written and the error names the hunk."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": (
                            "The full patch envelope, beginning with "
                            "*** Begin Patch and ending with *** End Patch. "
                            "Inside it every operation starts with one of "
                            "*** Add File: <path>, *** Update File: <path> "
                            "or *** Delete File: <path> -- spelled exactly "
                            "that way, since a near-miss is refused and "
                            "costs a turn. Every content line inside a "
                            "hunk carries a leading +, - or space; an Add "
                            "File body is therefore all + lines. To create "
                            "a whole new file, prefer write_file, which "
                            "takes the text as-is."
                        ),
                    }
                },
                "required": ["patch"],
            },
        },
    }


def read_file_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": READ_FILE,
            "description": ("Read one file from the workspace as text. Large files are truncated."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": ("Path relative to the workspace root."),
                    }
                },
                "required": ["path"],
            },
        },
    }


def write_file_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": WRITE_FILE,
            "description": (
                "Write one whole file into the workspace, creating parent "
                "directories and replacing any existing content. Use this "
                "to create a new file -- a script you are about to run, an "
                "input data file. Prefer apply_patch only when editing "
                "part of a file that already exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "The file's full text, written verbatim. No "
                            "patch envelope and no line prefixes."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
        },
    }


def list_files_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": LIST_FILES,
            "description": ("List every file currently in the workspace, relative to its root."),
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _poll_properties() -> dict[str, Any]:
    return {
        "session_id": {
            "type": "string",
            "description": "The id run_command returned.",
        },
        "cursor": {
            "type": "object",
            "description": (
                "The cursor from your previous reply. Omit to read the "
                "command's output from the start."
            ),
            "properties": {
                "stdout": {"type": "integer"},
                "stderr": {"type": "integer"},
            },
        },
        "wait_seconds": {
            "type": "number",
            "description": (
                "How long to wait for it to finish before replying. "
                f"Capped at {int(DEFAULT_COMMAND_TIMEOUT_SECONDS)} "
                "seconds; 0 to look and reply immediately."
            ),
        },
        "input": {
            "type": "string",
            "description": (
                "Text to send to the command's stdin. Include a trailing "
                "newline if it reads by line."
            ),
        },
        "kill": {
            "type": "boolean",
            "description": "End the command and its children.",
        },
    }


def poll_command_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": POLL_COMMAND,
            "description": (
                "Continue a command that run_command left running. "
                "Returns whatever it has written since your last cursor "
                "-- not from the start, so polling a chatty command does "
                "not re-read it -- and its exit code once it finishes. "
                "Use `input` to answer a prompt it is waiting on, and "
                "`kill` to end it."
            ),
            "parameters": {
                "type": "object",
                "properties": _poll_properties(),
                "required": ["session_id"],
            },
        },
    }


def read_skill_schema(names: tuple[str, ...]) -> dict[str, Any]:
    """Enumerate installed names so provider validation rejects invented skills
    before a wasted turn.
    """
    return {
        "type": "function",
        "function": {
            "name": READ_SKILL,
            "description": (
                "Read one science skill's full instructions: what it can "
                "do, the exact commands to run, and the mistakes to "
                "avoid. Call this before using a skill -- the catalogue "
                "gives you only a one-line summary of each. The "
                "instructions tell you which script to run; run it with "
                "run_command. Where those instructions point at a file "
                "under references/, call this again with that path to "
                "get it -- the exact command syntax is usually there. "
                "Each document is long and every later turn re-sends "
                "it, so read one skill and use it before considering "
                "another."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": list(names),
                        "description": "The skill name from the catalogue.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional file inside the skill, as its own "
                            "document names it, e.g. "
                            "references/interactions.md. Omit for the "
                            "skill's main instructions."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    }
