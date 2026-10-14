from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator

import pytest
from co_scientist.core.config import PROVIDER_CREDENTIAL_ENV
from co_scientist.platform.llm import process_mode

from ._process_mode_helpers import FakeProcessMode

# LiteLLM fetches this config from GitHub on its first Anthropic request in a
# process, so a test recording outbound requests saw it depending on order.
os.environ.setdefault("LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS", "True")

# Scrub credentials from the shared provider map so newly supported keys cannot
# leak into paid calls.
_PROVIDER_KEYS = tuple(name for names in PROVIDER_CREDENTIAL_ENV.values() for name in names)


@pytest.fixture(scope="session", autouse=True)
def _offline_router() -> None:
    from co_scientist.platform.llm.offline.llm import install_offline_router

    install_offline_router()


@pytest.fixture(autouse=True)
def _free_catalog(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


def _apply_offline_env(monkeypatch: pytest.MonkeyPatch, db_path: str) -> None:
    monkeypatch.setenv("COSCIENTIST_DB_PATH", db_path)
    for key in _PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COSCIENTIST_TEST_DOUBLE", "deterministic")
    monkeypatch.setenv("FORCE_LITERATURE_REVIEW", "0")
    from co_scientist.core.config import settings

    monkeypatch.setattr(settings, "evidence_resolver", "offline")
    monkeypatch.setattr(settings, "logs_admin_token", "synthetic-test-operator")


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Iterator[str]:
    db_path = str(tmp_path / "test.db")
    _apply_offline_env(monkeypatch, db_path)
    from co_scientist.platform import db as _store_db

    _store_db._initialized.discard(db_path)
    yield db_path
    # Stop log capture before environment teardown so queued records cannot
    # spill into another test database.
    from co_scientist.platform.db.log_capture import shutdown_log_capture

    shutdown_log_capture()


@pytest.fixture
def reachable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # The offline guard runs before transport; request-shape tests must declare
    # a reachable fake provider.
    monkeypatch.delenv("COSCIENTIST_TEST_DOUBLE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-placeholder-for-shape-tests")


@pytest.fixture
def fake_process_mode() -> Iterator[FakeProcessMode]:
    fake = FakeProcessMode()
    previous = process_mode.install(fake)
    yield fake
    process_mode.install(previous)


@pytest.fixture(autouse=True)
def _fresh_probe_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear module probe caches so earlier tests cannot supply status within the
    # TTL window.
    from co_scientist.api import diagnostics

    monkeypatch.setattr(diagnostics, "_probe_cache", None)
    monkeypatch.setattr(diagnostics, "_probe_refresh", None)


@pytest.fixture(autouse=True)
def _dispatch_open() -> None:
    # A lifespan shutdown or unanswered call in an earlier test is process-wide.
    from co_scientist.core import inflight

    inflight.resume_dispatch()
    inflight._unanswered.clear()


@pytest.fixture(autouse=True)
def _fresh_health_check_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Health caches reference prior isolated databases; clear them before each
    # test.
    from co_scientist.api import diagnostics

    monkeypatch.setattr(diagnostics, "_health_check_cache", None)


@pytest.fixture
def startup_recovery_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep startup lease recovery active without launching recovered work."""
    from co_scientist.orchestration import task_worker

    monkeypatch.setattr(task_worker, "run_run_worker_pool_sync", lambda *_: None)


@pytest.fixture
def manual_worker(monkeypatch: pytest.MonkeyPatch, startup_recovery_worker: None) -> None:
    """Keep manually stepped task leases out of startup recovery."""
    from co_scientist import main

    monkeypatch.setattr(main, "_expire_earlier_process_leases", lambda: None)
