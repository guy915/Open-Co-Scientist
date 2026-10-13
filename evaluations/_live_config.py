import os
import sys


def configure_live_environment(model: str | None = None) -> str:
    """Configure before app imports in a fresh process; live credentials must
    be explicit, never read from disk.
    """
    model = _explicit_model(model)
    for name in list(os.environ):
        if name.upper().endswith("_API_KEY") and name != "OPENROUTER_API_KEY":
            del os.environ[name]
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["COSCIENTIST_REQUIRE_FREE_MODELS"] = "1"
    for name in (
        "MODEL_NAME",
        "SUPERVISOR_MODEL_NAME",
        "CHAT_MODEL_NAME",
        "SEMANTIC_SAFETY_MODEL",
        "CLAIM_VERIFIER_MODEL",
    ):
        os.environ[name] = model
    return model


def _explicit_model(selected: str | None) -> str:
    if "co_scientist.core.config" in sys.modules:
        raise RuntimeError("live evaluation requires a fresh process")
    model = (selected if selected is not None else os.getenv("MODEL_NAME", "")).strip()
    if not model.startswith("openrouter/") or model == "openrouter/":
        raise ValueError("live evaluation requires explicit OpenRouter MODEL_NAME")
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        raise ValueError("live evaluation requires OPENROUTER_API_KEY")
    return model
