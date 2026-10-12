"""Stable run paths survive worker restarts; scratch stays off the database
volume. Untrusted IDs must never grant a writable root outside their run.
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from co_scientist.platform.sandbox.workspace.output import SecretRegistry
from co_scientist.platform.sandbox.workspace.session import WorkspaceSession
from co_scientist.platform.sandbox.workspace.tools import WorkspaceToolProvider

logger = logging.getLogger(__name__)

WORKSPACE_DIR_ENV = "COSCIENTIST_WORKSPACE_DIR"

_WORKSPACES_DIRNAME = "coscientist-workspaces"

_REVIEWS_DIRNAME = "reviews"
_DRAFTS_DIRNAME = "drafts"

# Run IDs are UUIDs; a narrow alphabet blocks path traversal.
_UNSAFE_IN_RUN_ID = re.compile(r"[^A-Za-z0-9_-]")

# Truncate from the back to retain UUID entropy at the front.
_MAX_RUN_ID_CHARS = 64


class WorkspaceIdError(ValueError):
    """An unusable ID must not collapse multiple runs onto the common workspace
    root.
    """


def _safe_run_id(run_id: str) -> str:
    """An empty sanitized ID would grant every run the same workspace."""
    cleaned = _UNSAFE_IN_RUN_ID.sub("_", run_id)[:_MAX_RUN_ID_CHARS]
    if not cleaned.strip("_"):
        raise WorkspaceIdError(f"run id {run_id!r} contains nothing usable as a directory name")
    if cleaned != run_id:
        logger.warning("run id %r was sanitized to %r for use as a path", run_id, cleaned)
    return cleaned


def workspaces_root() -> Path:
    override = os.getenv(WORKSPACE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(tempfile.gettempdir()).resolve() / _WORKSPACES_DIRNAME


def workspace_path(run_id: str) -> Path:
    return workspaces_root() / _safe_run_id(run_id)


def open_run_workspace(run_id: str, *, network_allowed: bool = False) -> WorkspaceSession:
    root = workspace_path(run_id)
    existed = root.exists()
    session = WorkspaceSession(root, network_allowed=network_allowed)
    # Owner-only permissions protect run inputs; unsupported filesystems must
    # not reject the run.
    try:
        root.chmod(0o700)
    except OSError as exc:  # pragma: no cover - filesystem-dependent
        logger.debug("could not restrict workspace permissions: %s", exc)
    logger.info(
        "%s workspace for run %s at %s",
        "reopened" if existed else "created",
        run_id,
        root,
    )
    return session


def draft_workspace_path(run_id: str, draft_id: str) -> Path:
    return workspace_path(run_id) / _DRAFTS_DIRNAME / _safe_run_id(draft_id)


def open_draft_workspace(run_id: str, draft_id: str) -> WorkspaceSession:
    """Each drafting pass gets fresh files so a later cycle cannot mistake
    stale results for its own.
    """
    root = draft_workspace_path(run_id, draft_id)
    # Restrict the parent before creating a child so it is never briefly world-
    # readable.
    open_run_workspace(run_id)
    return WorkspaceSession(root, skills_enabled=True)


def review_workspace_path(run_id: str, hypothesis_id: str) -> Path:
    return workspace_path(run_id) / _REVIEWS_DIRNAME / _safe_run_id(hypothesis_id)


def open_review_workspace(
    run_id: str, hypothesis_id: str, *, network_allowed: bool = False
) -> WorkspaceSession:
    """Concurrent hypothesis reviews must never read each other's simulation
    models.
    """
    root = review_workspace_path(run_id, hypothesis_id)
    # Restrict the parent before creating a child so it is never briefly world-
    # readable.
    open_run_workspace(run_id, network_allowed=network_allowed)
    return WorkspaceSession(root, network_allowed=network_allowed)


def build_workspace_tools(
    run_id: str,
    *,
    delegate: Any | None = None,
    network_allowed: bool = False,
    secrets: SecretRegistry | None = None,
) -> WorkspaceToolProvider:
    return WorkspaceToolProvider(
        open_run_workspace(run_id, network_allowed=network_allowed),
        delegate=delegate,
        secrets=secrets,
    )
