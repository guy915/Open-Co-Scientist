"""Context anchors reject stale edits instead of applying them at obsolete
line numbers.
"""

import errno
import logging
import os
import re
import secrets
import stat
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class LineKind(Enum):
    CONTEXT = " "
    REMOVE = "-"
    ADD = "+"


@dataclass(frozen=True)
class PatchLine:
    kind: LineKind
    text: str


@dataclass(frozen=True)
class Hunk:
    """Hunk headers are advisory: a guessed function name cannot veto
    matching context.
    """

    lines: tuple[PatchLine, ...]
    header: str | None = None

    @property
    def anchor(self) -> tuple[str, ...]:
        return tuple(
            line.text for line in self.lines if line.kind in (LineKind.CONTEXT, LineKind.REMOVE)
        )

    @property
    def replacement(self) -> tuple[str, ...]:
        return tuple(
            line.text for line in self.lines if line.kind in (LineKind.CONTEXT, LineKind.ADD)
        )


@dataclass(frozen=True)
class AddFile:
    path: str
    content: str


@dataclass(frozen=True)
class DeleteFile:
    path: str


@dataclass(frozen=True)
class UpdateFile:
    path: str
    hunks: tuple[Hunk, ...] = field(default_factory=tuple)
    move_to: str | None = None


FileOp = AddFile | DeleteFile | UpdateFile


@dataclass(frozen=True)
class Patch:
    operations: tuple[FileOp, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(op.path for op in self.operations)


class PatchError(ValueError):
    """Both parse and application failures give the model actionable feedback
    to correct its patch.
    """


_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = "*** Add File: "
_DELETE = "*** Delete File: "
_UPDATE = "*** Update File: "
_MOVE = "*** Move to: "
_EOF_MARKER = "*** End of File"

_HEADER_RE = re.compile(r"^@@(?: (?P<text>.*))?$")

_OPERATION_PREFIXES = (_ADD, _DELETE, _UPDATE)


def _is_operation_start(line: str) -> bool:
    return line.startswith(_OPERATION_PREFIXES) or line == _END


def _parse_body_line(line: str) -> PatchLine:
    if line == "":
        # A blank context line is " ", which trailing-whitespace
        # stripping removes somewhere between the model and here.
        return PatchLine(LineKind.CONTEXT, "")
    marker, text = line[0], line[1:]
    for kind in LineKind:
        if marker == kind.value:
            return PatchLine(kind, text)
    raise PatchError(
        f"unrecognized line in hunk: {line!r} (expected it to start with ' ', '-', or '+')"
    )


class _Cursor:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.index = 0

    @property
    def done(self) -> bool:
        return self.index >= len(self._lines)

    def peek(self) -> str:
        return self._lines[self.index]

    def take(self) -> str:
        line = self._lines[self.index]
        self.index += 1
        return line


def _parse_add(cursor: _Cursor, path: str) -> AddFile:
    body: list[str] = []
    while not cursor.done and not _is_operation_start(cursor.peek()):
        line = cursor.take()
        if not line.startswith("+"):
            raise PatchError(
                f"every line of an added file must start with '+'; got {line!r} in {path!r}"
            )
        body.append(line[1:])
    content = "\n".join(body)
    if body:
        content += "\n"
    return AddFile(path=path, content=content)


def _flush(hunks: list[Hunk], current: list[PatchLine], header: str | None) -> list[PatchLine]:
    if current:
        hunks.append(Hunk(tuple(current), header))
    return []


def _parse_move(cursor: _Cursor) -> str | None:
    if not cursor.done and cursor.peek().startswith(_MOVE):
        return cursor.take()[len(_MOVE) :].strip()
    return None


def _parse_update(cursor: _Cursor, path: str) -> UpdateFile:
    move_to = _parse_move(cursor)
    hunks: list[Hunk] = []
    current: list[PatchLine] = []
    header: str | None = None

    while not cursor.done and not _is_operation_start(cursor.peek()):
        line = cursor.take()
        if line == _EOF_MARKER:
            continue
        match = _HEADER_RE.match(line)
        if match:
            current = _flush(hunks, current, header)
            header = match.group("text")
            continue
        current.append(_parse_body_line(line))

    _flush(hunks, current, header)
    if not hunks:
        raise PatchError(f"update of {path!r} contains no changes")
    return UpdateFile(path=path, hunks=tuple(hunks), move_to=move_to)


def _parse_operation(cursor: _Cursor) -> FileOp:
    line = cursor.take()
    if line.startswith(_ADD):
        return _parse_add(cursor, line[len(_ADD) :].strip())
    if line.startswith(_DELETE):
        return DeleteFile(path=line[len(_DELETE) :].strip())
    return _parse_update(cursor, line[len(_UPDATE) :].strip())


def _envelope_lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines or lines[0].rstrip() != _BEGIN:
        raise PatchError(f"patch must start with {_BEGIN!r}")
    if lines[-1].rstrip() != _END:
        raise PatchError(f"patch must end with {_END!r}")
    return lines[1:-1]


def parse_patch(text: str) -> Patch:
    cursor = _Cursor(_envelope_lines(text))
    operations: list[FileOp] = []

    while not cursor.done:
        line = cursor.peek()
        if not line.strip():
            cursor.take()
            continue
        if not line.startswith(_OPERATION_PREFIXES):
            raise PatchError(
                f"expected a file operation, got {line!r}. Each operation "
                f"starts with one of {_OPERATION_PREFIXES}"
            )
        operations.append(_parse_operation(cursor))

    if not operations:
        raise PatchError("patch contains no file operations")
    return Patch(operations=tuple(operations))


# Fold smart punctuation so transit substitutions cannot invalidate otherwise
# matching context.
_PUNCTUATION_EQUIVALENTS = {
    # Escapes distinguish visually confusable punctuation.
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2032": "'",
    "\u2033": '"',
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u00a0": " ",
    "\u2026": "...",
}


def _normalize_punctuation(text: str) -> str:
    folded = unicodedata.normalize("NFC", text)
    for source, target in _PUNCTUATION_EQUIVALENTS.items():
        folded = folded.replace(source, target)
    return folded


# The ladder, most exact first. Each entry is a canonicalization applied
# to both sides before comparison; equality under it defines that rung.
_LADDER = (
    ("exact", lambda line: line),
    ("trailing-whitespace", lambda line: line.rstrip()),
    ("surrounding-whitespace", lambda line: line.strip()),
    (
        "unicode-punctuation",
        lambda line: _normalize_punctuation(line).strip(),
    ),
)


@dataclass(frozen=True)
class SeekResult:
    start: int
    end: int
    rung: str


def _matches_at(
    lines: list[str],
    anchor: tuple[str, ...],
    index: int,
    canonicalize: object,
) -> bool:
    fold = canonicalize
    assert callable(fold)
    return all(fold(lines[index + offset]) == fold(text) for offset, text in enumerate(anchor))


def _first_match_on_rung(
    lines: list[str],
    anchor: tuple[str, ...],
    start: int,
    rung: str,
    fold: object,
) -> SeekResult | None:
    last = len(lines) - len(anchor)
    for index in range(start, last + 1):
        if _matches_at(lines, anchor, index, fold):
            return SeekResult(index, index + len(anchor), rung)
    return None


def seek_anchor(lines: list[str], anchor: tuple[str, ...], start: int = 0) -> SeekResult | None:
    """Search from a monotonic cursor so repeated context blocks resolve in
    order.
    """
    if not anchor:
        return SeekResult(start, start, "empty")
    if len(lines) - len(anchor) < start:
        return None

    # Prefer exact matches anywhere over nearer approximate matches.
    for rung, fold in _LADDER:
        found = _first_match_on_rung(lines, anchor, start, rung, fold)
        if found is not None:
            return found
    return None


@dataclass(frozen=True)
class PlannedWrite:
    path: str
    content: str | None  # None means delete
    original_path: str | None = None  # set when the op renames
    create_only: bool = False
    require_existing: bool = False


@dataclass(frozen=True)
class ApplyResult:
    changed: tuple[str, ...]
    rungs: dict[str, tuple[str, ...]]


def _path_parts(relative: str) -> tuple[str, ...]:
    if os.path.isabs(relative):
        raise PatchError(f"patch paths must be relative; got {relative!r}")
    parts: list[str] = []
    for part in relative.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PatchError(
                    f"patch path {relative!r} escapes outside the workspace patch root"
                )
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise PatchError(f"patch path {relative!r} does not name a file")
    return tuple(parts)


def _root_fd(root: Path) -> int:
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _parent_fd(root_fd: int, parts: tuple[str, ...], *, create: bool = False) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, dir_fd=current)
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current,
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PatchError(
                        f"workspace path component {part!r} resolves outside or is a symlink"
                    ) from exc
                raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _check_leaf(parent_fd: int, name: str, *, required: bool) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise
        return None
    if stat.S_ISLNK(info.st_mode):
        raise PatchError(f"workspace path {name!r} resolves outside or is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise PatchError(f"workspace path {name!r} is not a regular file")
    return info


def read_workspace_bytes(root: Path, relative: str, max_bytes: int | None = None) -> bytes:
    """Read a regular workspace file without following symlink components."""
    parts = _path_parts(relative)
    root_fd = _root_fd(root)
    parent_fd = -1
    try:
        parent_fd = _parent_fd(root_fd, parts)
        _check_leaf(parent_fd, parts[-1], required=True)
        fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PatchError(f"workspace path {relative!r} is not a regular file")
            chunks: list[bytes] = []
            remaining = max_bytes
            while remaining is None or remaining > 0:
                chunk = os.read(fd, 64 * 1024 if remaining is None else min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)


def read_workspace_file(root: Path, relative: str) -> str:
    return read_workspace_bytes(root, relative).decode("utf-8")


def _temporary_file(parent_fd: int, content: str, mode: int | None) -> str:
    name = f".coscientist-{secrets.token_hex(12)}"
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o666,
        dir_fd=parent_fd,
    )
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent_fd)
        raise
    return name


def _write_workspace_file(
    root_fd: int,
    relative: str,
    content: str,
    *,
    create_only: bool,
    require_existing: bool,
    mode: int | None = None,
) -> None:
    parts = _path_parts(relative)
    parent_fd = _parent_fd(root_fd, parts, create=True)
    temp_name: str | None = None
    try:
        existing = _check_leaf(parent_fd, parts[-1], required=False)
        if create_only and existing is not None:
            raise PatchError(f"cannot add {relative!r}: it already exists")
        if require_existing and existing is None:
            raise FileNotFoundError(relative)
        temp_name = _temporary_file(
            parent_fd,
            content,
            (existing.st_mode & 0o777) if existing is not None else mode,
        )
        if create_only:
            os.link(
                temp_name,
                parts[-1],
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temp_name, dir_fd=parent_fd)
        else:
            _check_leaf(parent_fd, parts[-1], required=False)
            os.replace(temp_name, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temp_name = None
    finally:
        if temp_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temp_name, dir_fd=parent_fd)
        os.close(parent_fd)


def _delete_workspace_file(root_fd: int, relative: str) -> None:
    parts = _path_parts(relative)
    parent_fd = _parent_fd(root_fd, parts)
    try:
        _check_leaf(parent_fd, parts[-1], required=True)
        os.unlink(parts[-1], dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _workspace_file_mode(root_fd: int, relative: str) -> int:
    parts = _path_parts(relative)
    parent_fd = _parent_fd(root_fd, parts)
    try:
        _check_leaf(parent_fd, parts[-1], required=True)
        fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                raise PatchError(f"workspace path {relative!r} is not a regular file")
            return mode & 0o777
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def write_workspace_file(root: Path, relative: str, content: str) -> None:
    """Atomically write beneath root while refusing symlink path components."""
    root_fd = _root_fd(root)
    try:
        _write_workspace_file(
            root_fd,
            relative,
            content,
            create_only=False,
            require_existing=False,
        )
    finally:
        os.close(root_fd)


def _read_lines_from_text(text: str) -> list[str]:
    lines = text.split("\n")
    # Drop the trailing split element, then restore the newline on write to
    # match hunk line counts.
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _plan_update(root: Path, op: UpdateFile) -> tuple[PlannedWrite, tuple[str, ...]]:
    source = "/".join(_path_parts(op.path))
    try:
        lines = _read_lines_from_text(read_workspace_file(root, source))
    except FileNotFoundError as exc:
        raise PatchError(f"cannot update {op.path!r}: file does not exist") from exc
    cursor = 0
    rungs: list[str] = []

    for number, hunk in enumerate(op.hunks, start=1):
        found = seek_anchor(lines, hunk.anchor, cursor)
        if found is None:
            first = hunk.anchor[0] if hunk.anchor else "<empty>"
            raise PatchError(
                f"hunk {number} of {op.path!r} did not match: no occurrence "
                f"of its context at or after line {cursor + 1}. First "
                f"expected line was {first!r}. The file may have changed "
                f"since it was read."
            )
        replacement = list(hunk.replacement)
        lines[found.start : found.end] = replacement
        cursor = found.start + len(replacement)
        rungs.append(found.rung)

    target = "/".join(_path_parts(op.move_to)) if op.move_to else source
    content = "\n".join(lines) + "\n" if lines else ""
    return (
        PlannedWrite(
            path=target,
            content=content,
            original_path=source if op.move_to else None,
            require_existing=not bool(op.move_to),
        ),
        tuple(rungs),
    )


def _plan_operation(root: Path, op: FileOp) -> tuple[PlannedWrite, tuple[str, ...]]:
    if isinstance(op, AddFile):
        target = "/".join(_path_parts(op.path))
        return PlannedWrite(path=target, content=op.content, create_only=True), ()

    if isinstance(op, DeleteFile):
        target = "/".join(_path_parts(op.path))
        try:
            read_workspace_file(root, target)
        except FileNotFoundError as exc:
            raise PatchError(f"cannot delete {op.path!r}: file does not exist") from exc
        return PlannedWrite(path=target, content=None), ()

    return _plan_update(root, op)


def _commit(root_fd: int, write: PlannedWrite) -> None:
    if write.content is None:
        _delete_workspace_file(root_fd, write.path)
        return
    _write_workspace_file(
        root_fd,
        write.path,
        write.content,
        create_only=write.create_only,
        require_existing=write.require_existing,
        mode=(
            _workspace_file_mode(root_fd, write.original_path)
            if write.original_path and write.original_path != write.path
            else None
        ),
    )
    if write.original_path and write.original_path != write.path:
        _delete_workspace_file(root_fd, write.original_path)


def apply_patch(patch: Patch, root: Path) -> ApplyResult:
    """Plan every operation before writing, so an unresolved operation leaves
    the tree unchanged.
    """
    plans: list[PlannedWrite] = []
    rungs: dict[str, tuple[str, ...]] = {}

    for op in patch.operations:
        write, matched = _plan_operation(root, op)
        plans.append(write)
        if matched:
            rungs[op.path] = matched

    root_fd = _root_fd(root)
    try:
        for write in plans:
            _commit(root_fd, write)
    finally:
        os.close(root_fd)

    changed = tuple(write.path for write in plans)
    logger.debug("applied patch touching %s", changed)
    return ApplyResult(changed=changed, rungs=rungs)


__all__ = [
    "AddFile",
    "ApplyResult",
    "DeleteFile",
    "Hunk",
    "LineKind",
    "Patch",
    "PatchError",
    "PatchLine",
    "SeekResult",
    "UpdateFile",
    "apply_patch",
    "parse_patch",
    "read_workspace_bytes",
    "read_workspace_file",
    "seek_anchor",
    "write_workspace_file",
]
