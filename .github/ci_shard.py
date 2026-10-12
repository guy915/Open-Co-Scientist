"""Pytest plugin that keeps one CI shard of the collected tests.

Load with ``-p ci_shard`` and set CI_SHARD_INDEX / CI_SHARD_TOTAL. Round-robin
over sorted node IDs spreads one slow module across shards, and the selection
is identical in every xdist worker, which xdist requires.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    total = int(os.environ.get("CI_SHARD_TOTAL", "1"))
    index = int(os.environ.get("CI_SHARD_INDEX", "0"))
    if total <= 1:
        return
    if not 0 <= index < total:
        raise pytest.UsageError(f"CI_SHARD_INDEX {index} outside 0..{total - 1}")
    ordered = sorted(items, key=lambda item: item.nodeid)
    keep = {item.nodeid for item in ordered[index::total]}
    deselected = [item for item in items if item.nodeid not in keep]
    items[:] = [item for item in items if item.nodeid in keep]
    config.hook.pytest_deselected(items=deselected)
