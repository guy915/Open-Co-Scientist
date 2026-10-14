import faulthandler
import pathlib
import signal
import sys
from collections.abc import Iterator
from types import FrameType
from typing import Any, ClassVar

import pytest

# CI cancels the engine job at 15 minutes with no stacks. A hung test fails
# first and prints every thread's stack; the hard exit covers uninterruptible hangs.
_HANG_SECONDS = 300


@pytest.fixture(autouse=True)
def _fail_a_hung_test() -> Iterator[None]:
    def _expire(signum: int, frame: FrameType | None) -> None:
        faulthandler.dump_traceback(file=2, all_threads=True)
        pytest.fail(
            f"test still running after {_HANG_SECONDS} s; thread stacks are in its stderr",
            pytrace=False,
        )

    previous = signal.signal(signal.SIGALRM, _expire)
    signal.alarm(_HANG_SECONDS)
    faulthandler.dump_traceback_later(_HANG_SECONDS + 60, exit=True)
    try:
        yield
    finally:
        signal.alarm(0)
        faulthandler.cancel_dump_traceback_later()
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture(autouse=True)
def _isolated_admission_store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSCIENTIST_DB_PATH", str(tmp_path / "admission.db"))


# Some editable installs fail to process the .pth file; keep imports working.
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def clear_capability_cache() -> Iterator[None]:
    """Memoized per-model answers must not leak between patched providers."""
    from co_scientist.platform.llm.request.backend import _registry_supports_json_schema

    _registry_supports_json_schema.cache_clear()
    yield
    _registry_supports_json_schema.cache_clear()


@pytest.fixture
def _patch_mcp_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[type[Any]]:
    """Fresh fake classes and a reset global client prevent counters and
    tools leaking between tests."""
    from langchain_core.tools import StructuredTool

    import co_scientist.platform.retrieval.mcp_client as mcp_session_mod
    from tests._mcp import FakeMultiServerMCPClient

    class _Fake(FakeMultiServerMCPClient):
        instances_created = 0
        tools: ClassVar[list[StructuredTool]] = []
        error: Exception | None = None

    monkeypatch.setattr(mcp_session_mod, "MultiServerMCPClient", _Fake)
    with mcp_session_mod._global_clients_lock:
        mcp_session_mod._global_clients.clear()
    yield _Fake
    with mcp_session_mod._global_clients_lock:
        mcp_session_mod._global_clients.clear()


@pytest.fixture(autouse=True)
def _free_catalog(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Synthetic model metadata keeps mocked free requests hermetic."""
    from co_scientist.platform.llm.admission import free_policy as free_catalog
    from co_scientist.platform.llm.profile import MODEL_PRICING

    catalog = {
        model.removeprefix("openrouter/"): {
            "pricing": {
                "prompt": str(price.prompt_usd_per_million),
                "completion": str(price.completion_usd_per_million),
            },
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        }
        for model, price in MODEL_PRICING.items()
        if model.startswith("openrouter/")
    }
    monkeypatch.delenv("COSCIENTIST_REQUIRE_FREE_MODELS", raising=False)
    monkeypatch.setattr(free_catalog, "_reader", free_catalog.CatalogReader(lambda: catalog))
    yield
