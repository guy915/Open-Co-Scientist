"""Optional operator-supplied DOI list supplements source retraction flags.
No third-party retraction dataset is distributed with this package.
"""

from __future__ import annotations

import functools
import gzip
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DOI_URL_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi.org/")


def normalize_doi(doi: str) -> str:
    text = doi.strip().lower()
    for prefix in _DOI_URL_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


@functools.lru_cache(maxsize=4)
def _load_doi_set(path: Path) -> frozenset[str]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return frozenset(line.strip() for line in handle if line.strip())
    except OSError as exc:
        logger.warning("Retraction dataset unreadable at %s: %s", path, exc)
        return frozenset()


def is_known_retracted(doi: str, *, path: Path | None = None) -> bool:
    if not doi:
        return False
    if path is None:
        configured = os.environ.get("COSCIENTIST_RETRACTIONS_PATH", "").strip()
        if not configured:
            return False
        path = Path(configured)
    return normalize_doi(doi) in _load_doi_set(path)
