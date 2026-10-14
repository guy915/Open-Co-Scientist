"""Use stdlib parsing: production lacks dev linters. Findings cannot veto
writes because ordinary multi-step edits may temporarily fail to parse.
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from co_scientist.platform.sandbox.patch import PatchError, read_workspace_bytes

logger = logging.getLogger(__name__)


# Beyond this size parsing data costs more than feedback is worth.
MAX_CHECKED_BYTES = 2_000_000

# Bound findings even when a patch rewrites a whole tree.
MAX_FINDINGS = 20


@dataclass(frozen=True)
class CheckFinding:
    path: str
    line: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": self.path, "message": self.message}
        if self.line is not None:
            payload["line"] = self.line
        return payload


def _check_python(source: str) -> tuple[int | None, str] | None:
    try:
        compile(source, "<workspace>", "exec")
    except SyntaxError as exc:
        return exc.lineno, f"SyntaxError: {exc.msg}"
    except ValueError as exc:
        # compile() rejects null bytes without SyntaxError.
        return None, f"not valid Python source: {exc}"
    return None


def _check_json(source: str) -> tuple[int | None, str] | None:
    try:
        json.loads(source)
    except ValueError as exc:
        line = getattr(exc, "lineno", None)
        return line, f"invalid JSON: {exc}"
    return None


def _check_yaml(source: str) -> tuple[int | None, str] | None:
    import yaml

    try:
        yaml.safe_load(source)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        return line, f"invalid YAML: {getattr(exc, 'problem', exc)}"
    return None


_CHECKERS = {
    ".py": _check_python,
    ".json": _check_json,
    ".yaml": _check_yaml,
    ".yml": _check_yaml,
}


def _read_source(root: Path, relative: str) -> str | None:
    try:
        raw = read_workspace_bytes(root, relative, max_bytes=MAX_CHECKED_BYTES + 1)
        if len(raw) > MAX_CHECKED_BYTES:
            return None
        return raw.decode("utf-8")
    except (OSError, PatchError, UnicodeDecodeError):
        # Deleted/nontext files are not findings; report only parser rejections.
        return None


def _check_one(root: Path, relative: str) -> CheckFinding | None:
    checker = _CHECKERS.get(Path(relative).suffix.lower())
    if checker is None:
        return None
    source = _read_source(root, relative)
    if source is None:
        return None
    problem = checker(source)
    if problem is None:
        return None
    line, message = problem
    return CheckFinding(relative, line, message)


def check_paths(root: Path, paths: Iterable[str]) -> tuple[CheckFinding, ...]:
    findings: list[CheckFinding] = []
    for relative in paths:
        finding = _check_one(root, relative)
        if finding is not None:
            findings.append(finding)
        if len(findings) >= MAX_FINDINGS:
            break
    return tuple(findings)
