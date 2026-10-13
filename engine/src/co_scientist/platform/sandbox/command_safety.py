"""Approval classification is not confinement; unparsed shell constructs are
unsafe. Ported from OpenAI Codex CLI is_safe_command.rs (Apache-2.0).
"""

import logging
import shlex
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

_ALWAYS_SAFE = frozenset(
    {
        "cat",
        "cd",
        "cut",
        "date",
        "df",
        "du",
        "echo",
        "expr",
        "false",
        "file",
        "grep",
        "head",
        "hostname",
        "id",
        "ls",
        "nl",
        "paste",
        "printenv",
        "pwd",
        "rev",
        "seq",
        "sort",
        "stat",
        "tail",
        "tr",
        "true",
        "uname",
        "uniq",
        "wc",
        "which",
        "whoami",
    }
)

_CONDITIONALLY_SAFE: dict[str, frozenset[str]] = {
    # find execution/deletion/output flags break the read-only classification.
    "find": frozenset(
        {
            "-exec",
            "-execdir",
            "-ok",
            "-okdir",
            "-delete",
            "-fls",
            "-fprint",
            "-fprint0",
            "-fprintf",
        }
    ),
    # base64 output flags write decoded data.
    "base64": frozenset({"-o", "--output"}),
}

# Joined output flags evade exact-flag matching.
_CONDITIONAL_PREFIXES: dict[str, tuple[str, ...]] = {
    "base64": ("--output=", "-o"),
}

_SEQUENCING_OPERATORS = ("&&", "||", ";", "|")

# Unsupported syntax is unsafe, even without a known dangerous operation.
_UNPARSED_CONSTRUCTS = ("$(", "`", ">", "<", "&", "\n", "$((", "${")

_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})

_SHELL_SCRIPT_FLAGS = frozenset({"-c", "-lc", "-lic", "-ic"})


def _basename(command: str) -> str:
    return PurePosixPath(command).name


def _flags_disqualify(name: str, args: list[str]) -> bool:
    unsafe_flags = _CONDITIONALLY_SAFE.get(name, frozenset())
    if any(arg in unsafe_flags for arg in args):
        return True
    prefixes = _CONDITIONAL_PREFIXES.get(name, ())
    return any(arg.startswith(prefix) and arg != prefix for arg in args for prefix in prefixes)


def _is_safe_simple_command(argv: list[str]) -> bool:
    if not argv:
        return False
    name = _basename(argv[0])
    if name in _ALWAYS_SAFE:
        return True
    if name in _CONDITIONALLY_SAFE:
        return not _flags_disqualify(name, argv[1:])
    return False


def _split_on_operators(script: str) -> list[str] | None:
    """Split sequencing operators before rejecting overlapping constructs; ||
    precedes |.
    """
    segments = [script]
    for operator in _SEQUENCING_OPERATORS:
        expanded: list[str] = []
        for segment in segments:
            expanded.extend(segment.split(operator))
        segments = expanded

    stripped = [segment.strip() for segment in segments if segment.strip()]
    if any(token in segment for segment in stripped for token in _UNPARSED_CONSTRUCTS):
        return None
    return stripped


def _shell_script(argv: list[str]) -> str | None:
    if len(argv) < 3 or _basename(argv[0]) not in _SHELLS:
        return None
    if argv[1] not in _SHELL_SCRIPT_FLAGS:
        return None
    # Extra shell arguments introduce behavior this classifier does not model.
    return argv[2] if len(argv) == 3 else None


def _is_safe_shell_invocation(script: str) -> bool:
    segments = _split_on_operators(script)
    if not segments:
        return False
    try:
        parsed = [shlex.split(segment) for segment in segments]
    except ValueError:
        return False
    return all(_is_safe_simple_command(argv) for argv in parsed)


def is_known_safe(argv: list[str]) -> bool:
    if not argv:
        return False
    if _is_safe_simple_command(argv):
        return True

    script = _shell_script(argv)
    if script is None:
        return False
    return _is_safe_shell_invocation(script)
