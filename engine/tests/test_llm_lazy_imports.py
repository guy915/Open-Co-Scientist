from __future__ import annotations

import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from co_scientist.platform import llm
from co_scientist.platform.llm.request import backend


def test_budget_and_reservation_imports_do_not_load_the_provider_sdk() -> None:
    code = """
import importlib.abc
import socket
import sys

class NoProviderSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "litellm":
            raise AssertionError("budget-only import loaded the provider SDK")
        return None

def no_network(*args, **kwargs):
    raise AssertionError("budget imports must not access the network")

sys.meta_path.insert(0, NoProviderSDK())
socket.socket.connect = no_network
from co_scientist.platform.llm.admission.call_budget import scoped_llm_call_budget
from co_scientist.platform.llm.admission.service import reserve_physical
assert callable(scoped_llm_call_budget)
assert callable(reserve_physical)
assert "litellm" not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()


async def test_sdk_patch_seam_is_shared_with_the_request_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {"answer": "synthetic response"}
    fake = AsyncMock(return_value=answer)
    monkeypatch.setattr(llm.litellm, "acompletion", fake)

    result = await backend.LitellmBackend().complete(model="mock", messages=[])

    assert result is answer
    assert llm.litellm is sys.modules["litellm"]
    assert llm.litellm.suppress_debug_info is True
    fake.assert_awaited_once_with(model="mock", messages=[], num_retries=0, max_retries=0)


def test_direct_backend_import_suppresses_the_provider_debug_banner() -> None:
    code = """
import socket
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
from co_scientist.platform.llm.request import backend
import litellm
assert litellm.suppress_debug_info is True
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode()
