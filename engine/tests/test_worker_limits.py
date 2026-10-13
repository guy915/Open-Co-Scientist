from __future__ import annotations

import resource
import sys

import pytest

from co_scientist.domains.documents.worker_limits import set_resource_limits


def _applied_limits(monkeypatch: pytest.MonkeyPatch, platform: str) -> set[int]:
    applied: set[int] = set()
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(resource, "setrlimit", lambda limit, _value: applied.add(limit))
    set_resource_limits()
    return applied


def test_linux_workers_cap_cpu_memory_output_and_files(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _applied_limits(monkeypatch, "linux") == {
        resource.RLIMIT_CPU,
        resource.RLIMIT_AS,
        resource.RLIMIT_FSIZE,
        resource.RLIMIT_NOFILE,
    }


def test_darwin_workers_skip_only_the_address_space_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _applied_limits(monkeypatch, "darwin") == {
        resource.RLIMIT_CPU,
        resource.RLIMIT_FSIZE,
        resource.RLIMIT_NOFILE,
    }
