"""Artifacts need source, environment and prompt provenance to make
measurements reproducible.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import platform
import subprocess
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_RESULTS_DIR = _ROOT / "evaluations" / "results"
_PROMPTS_DIR = _ROOT / "engine" / "src" / "co_scientist" / "science" / "prompts" / "templates"

# Missing or slow Git must yield unknown provenance, never stall an evaluation.
_GIT_TIMEOUT_SECONDS = 5


def _git(*args: str) -> str | None:
    """Git failures yield unknown provenance rather than failing the
    evaluation.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _source_provenance() -> dict[str, Any]:
    """Unknown Git status must never imply a confirmed clean checkout."""
    status = _git("status", "--porcelain")
    return {
        "git_commit": _git("rev-parse", "HEAD") or "unknown",
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "git_dirty": bool(status) if status is not None else None,
    }


def _environment_provenance() -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }


def _prompt_identity() -> dict[str, Any]:
    if not _PROMPTS_DIR.is_dir():
        return {"templates_dir": None, "digest": None, "file_count": 0}
    files = sorted(_PROMPTS_DIR.rglob("*.md"))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(_PROMPTS_DIR).as_posix().encode())
        digest.update(path.read_bytes())
    return {
        "templates_dir": str(_PROMPTS_DIR.relative_to(_ROOT)),
        "digest": digest.hexdigest(),
        "file_count": len(files),
    }


def build_provenance(
    *,
    model: Any = None,
    seed: Any = None,
    cost: Any = None,
) -> dict[str, Any]:
    return {
        "source": _source_provenance(),
        "environment": _environment_provenance(),
        "prompts": _prompt_identity(),
        "model": model,
        "seed": seed,
        "cost": cost,
        "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def write_dated_artifact(
    report: dict[str, Any],
    filename_stem: str,
    *,
    model: Any = None,
    seed: Any = None,
    cost: Any = None,
) -> pathlib.Path:
    stamped = {
        **report,
        "provenance": build_provenance(model=model, seed=seed, cost=cost),
    }
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    out = _RESULTS_DIR / f"{filename_stem}-{date}.json"
    out.write_text(json.dumps(stamped, indent=2) + "\n", encoding="utf-8")
    return out.relative_to(_ROOT)
