from __future__ import annotations

import os
from typing import Protocol

__all__ = [
    "EnvProcessMode",
    "ProcessMode",
    "credential_available",
    "install",
    "offline_mode",
    "production_routing_enabled",
]


class ProcessMode(Protocol):
    def is_offline(self) -> bool:
        """A partially configured deployment is real-backed; individual
        calls still require their own provider credential.
        """
        ...

    def credential_available(self, model: str) -> bool:
        """The shared provider table avoids drifting private copies; scoped
        BYOK also authorizes its own provider.
        """
        ...


class EnvProcessMode:
    def is_offline(self) -> bool:
        """A partially configured deployment is real-backed; individual
        calls still require their own provider credential.
        """
        return False

    def credential_available(self, model: str) -> bool:
        """The shared provider table avoids drifting private copies; scoped
        BYOK also authorizes its own provider.
        """
        # Import lazily so this leaf stays available from config without
        # credential-module cycles.
        from co_scientist.core import byok_scope
        from co_scientist.core.config import has_provider_credential

        if byok_scope.current_byok() is not None or has_provider_credential(model):
            return True
        from co_scientist.core.exceptions import ProviderAdmissionError
        from co_scientist.platform.llm.admission.service import current_db_path
        from co_scientist.platform.llm.request.backend import active_backend
        from co_scientist.platform.llm.routing import available_slots

        if not getattr(active_backend(), "operator_routing", False):
            return False
        try:
            return bool(available_slots(current_db_path()).slots)
        except ProviderAdmissionError:
            return False


class DeterministicTestMode:
    def is_offline(self) -> bool:
        return True

    def credential_available(self, model: str) -> bool:
        from co_scientist.core import byok_scope

        return byok_scope.current_byok() is not None


_current: ProcessMode = EnvProcessMode()
_test_double = DeterministicTestMode()


def _adapter() -> ProcessMode:
    if (
        isinstance(_current, EnvProcessMode)
        and os.getenv("COSCIENTIST_TEST_DOUBLE") == "deterministic"
    ):
        return _test_double
    return _current


def install(mode: ProcessMode) -> ProcessMode:
    """Call-time adapter lookup lets tests state one process fact; callers
    restore the replaced adapter after use.
    """
    global _current
    previous, _current = _current, mode
    return previous


def offline_mode() -> bool:
    """Use current process mode only for new execution, never to infer
    historical backend provenance.
    """
    return _adapter().is_offline()


def production_routing_enabled() -> bool:
    return isinstance(_adapter(), EnvProcessMode)


def credential_available(model: str) -> bool:
    """The shared provider table avoids drifting private copies; scoped BYOK
    also authorizes its own provider.
    """
    return _adapter().credential_available(model)
