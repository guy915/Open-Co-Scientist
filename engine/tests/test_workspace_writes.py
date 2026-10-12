from __future__ import annotations

from pathlib import Path

import pytest

import co_scientist.platform.sandbox.patch as patch_module
from co_scientist.platform.sandbox.patch import (
    FileOp,
    PatchError,
    PlannedWrite,
    apply_patch,
    parse_patch,
)
from co_scientist.platform.sandbox.workspace import WorkspaceSession


def _patch(*lines: str) -> str:
    return "\n".join(("*** Begin Patch", *lines, "*** End Patch"))


def test_workspace_write_rejects_symlinked_parent(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PatchError):
        WorkspaceSession(root).write_file("linked/new.txt", "payload")

    assert list(outside.iterdir()) == []


def test_patch_mutations_reject_symlinked_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text("old\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    operations = (
        _patch("*** Add File: linked/new.txt", "+new"),
        _patch("*** Delete File: linked/kept.txt"),
        _patch("*** Update File: linked/kept.txt", "@@", "-old", "+new"),
        _patch("*** Update File: local.txt", "*** Move to: linked/moved.txt", "@@", "-old", "+new"),
    )
    (root / "local.txt").write_text("old\n")

    for operation in operations:
        with pytest.raises((OSError, PatchError)):
            apply_patch(parse_patch(operation), root)

    assert (outside / "kept.txt").read_text() == "old\n"
    assert sorted(path.name for path in outside.iterdir()) == ["kept.txt"]
    assert (root / "local.txt").read_text() == "old\n"


def test_ancestor_swap_after_planning_cannot_redirect_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    directory = root / "target"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    original = patch_module._plan_operation
    swapped = False

    def plan_then_swap(patch_root: Path, operation: FileOp) -> tuple[PlannedWrite, tuple[str, ...]]:
        nonlocal swapped
        result = original(patch_root, operation)
        if not swapped:
            directory.rename(root / "held")
            directory.symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(patch_module, "_plan_operation", plan_then_swap)
    operation = parse_patch(_patch("*** Add File: target/new.txt", "+payload"))

    with pytest.raises(PatchError):
        apply_patch(operation, root)

    assert list(outside.iterdir()) == []
    assert not (root / "held" / "new.txt").exists()


def test_patch_create_update_delete_move_and_dotdot_semantics(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "old.txt").write_text("old\n")
    (root / "nested" / "old.txt").chmod(0o751)

    apply_patch(
        parse_patch(
            _patch(
                "*** Update File: nested/../nested/old.txt",
                "*** Move to: nested/new.txt",
                "@@",
                "-old",
                "+new",
            )
        ),
        root,
    )
    assert (root / "nested" / "new.txt").stat().st_mode & 0o777 == 0o751
    apply_patch(parse_patch(_patch("*** Add File: created/deep.txt", "+created")), root)
    apply_patch(parse_patch(_patch("*** Delete File: nested/new.txt")), root)

    assert not (root / "nested" / "new.txt").exists()
    assert (root / "created" / "deep.txt").read_text() == "created\n"
