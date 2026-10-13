# Subprocess probes need the recording backend supplied as script text.

from __future__ import annotations

from pathlib import Path

_ENGINE_FAKE = Path(__file__).resolve().parents[2] / "engine" / "tests" / "_llm_fake.py"

# Deferred imports let each probe configure its environment before loading
# the recording backend.
SCRIPT_PRELUDE = f"""
import os
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS"] = "True"

def fake_backend(provider):
    import importlib.util
    import sys
    from co_scientist.platform.llm.request.backend import using_backend

    spec = importlib.util.spec_from_file_location(
        "engine_llm_fake", {str(_ENGINE_FAKE)!r})
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return using_backend(module.FakeBackend(provider))
"""
