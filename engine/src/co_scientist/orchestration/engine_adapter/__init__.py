from __future__ import annotations

import os
import sys
from typing import Any

from co_scientist.core.config import any_provider_credential, byok_enabled, settings
from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import runs
from co_scientist.platform.db.spend import UNAVAILABLE
from co_scientist.platform.llm.process_mode import offline_mode, production_routing_enabled
from co_scientist.platform.retrieval.connectors import (
    connectors_report as connectors_report,
)
from co_scientist.platform.retrieval.connectors import enabled_tools

# Some Python 3.12 venvs omit editable-install .pth processing; durable tasks
# still need the sibling engine source.
_engine_src = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if os.path.isdir(_engine_src) and _engine_src not in sys.path:
    sys.path.insert(0, _engine_src)


# find_spec avoids engine import side effects on repeatedly polled availability
# probes.
def engine_importable() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("co_scientist") is not None
    except Exception:
        return False


def select_provider() -> str:
    """The engine is a hard dependency; fail at startup rather than silently
    changing scientific behavior.
    """
    if not engine_importable():
        raise RuntimeError(
            "co_scientist engine is not importable; it is a hard dependency "
            "of the app now that the mock provider has been retired"
        )
    return "engine"


def resolve_offline_backend(cfg: dict[str, Any]) -> bool:
    """Explicit backend choices win; validated BYOK credentials must never
    be shadowed by the offline router.
    """
    backend = cfg.get("llm_backend")
    if backend == "offline":
        if production_routing_enabled():
            raise ProviderAdmissionError(UNAVAILABLE)
        return True
    if backend == "real":
        return False
    if cfg.get("byok_provider"):
        return False
    return offline_mode()


def sync_engine_llm_backend(run_id: str, cfg: dict[str, Any], db_path: str | None) -> None:
    """Persist backend provenance before dispatch so later readers agree
    with the actual execution route.
    """
    backend = "offline" if resolve_offline_backend(cfg) else "real"
    runs.set_run_llm_backend(run_id, backend, db_path=db_path)


def system_status() -> dict[str, Any]:
    has_key = any_provider_credential()
    engine = engine_importable()
    provider = select_provider()
    return {
        "provider": provider,
        "llm_backend": "offline" if offline_mode() else "real",
        "has_provider_key": has_key,
        # Expose credential capability only, never credential material.
        "byok_enabled": byok_enabled(),
        "engine_importable": engine,
        "model_name": settings.model_name,
        # Diagnostics mirror the generator's actual supervisor-model fallback.
        "supervisor_model_name": settings.effective_supervisor_model,
        "mcp_server_url": settings.mcp_server_url,
        "enabled_tools": enabled_tools(),
    }


ENGINE_CHECKPOINT_PROVIDER = "engine"


def is_engine_checkpoint(checkpoint: dict[str, Any] | None) -> bool:
    if not checkpoint:
        return False
    state = checkpoint.get("state")
    return isinstance(state, dict) and state.get("provider") == ENGINE_CHECKPOINT_PROVIDER
