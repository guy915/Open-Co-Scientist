from __future__ import annotations

import os
import shutil
from pathlib import Path

from mcp_server.pubmed_storage import confined_path

PUBLIC_PAPERS = ".public-papers-v1"


def purge_legacy_pubmed_cache(root: Path) -> int:
    pubmed = confined_path(root, "pubmed")
    if not pubmed.exists():
        return 0
    public = pubmed / PUBLIC_PAPERS
    if public.is_symlink():
        raise ValueError("public paper cache must not be a symlink")
    removed = 0
    for entry in pubmed.iterdir():
        if entry.name == PUBLIC_PAPERS:
            continue
        # Never follow a legacy run link or topic-directory symlink outside
        # this dedicated cache; unlinking the link erases only its association.
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink()
        else:
            shutil.rmtree(entry)
        removed += 1
    return removed


def migrate_literature_cache() -> None:
    root = Path(os.getenv("COSCIENTIST_LIT_REVIEW_DIR", "./cache/literature_review"))
    try:
        purge_legacy_pubmed_cache(root)
    except (OSError, ValueError):
        # Directory names can contain private topic text, including in OS errors.
        raise RuntimeError("Legacy literature cache cleanup failed; tools cannot start") from None


if __name__ == "__main__":
    migrate_literature_cache()
