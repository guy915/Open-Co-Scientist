from __future__ import annotations

from typing import Any

from co_scientist.core.config import settings
from co_scientist.platform.llm.profile import is_free_route

# Engine tasks of a run stamped with this key admit only zero-price requests,
# so a lost lease can replay without an unknown spend.
ZERO_COST_CONFIG_KEY = "zero_cost_admission"


def zero_cost_admission_for_config(config: Any = None) -> bool:
    return isinstance(config, dict) and config.get(ZERO_COST_CONFIG_KEY) is True


def deployment_routes_are_free() -> bool:
    models = (
        settings.model_name,
        settings.effective_supervisor_model,
        settings.claim_verifier_model,
        settings.semantic_safety_model,
    )
    return all(is_free_route(model) for model in models if model)
