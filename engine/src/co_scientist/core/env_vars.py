import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


def _substitute_env_vars_in_string(value: str) -> str:
    pattern = r"\$\{([^}:]+)(?::-([^}]*))?\}"

    def replacer(match: "re.Match[str]") -> str:
        var_name = match.group(1)
        default = match.group(2)
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        logger.warning("environment variable %s not set and no default provided", var_name)
        return ""

    return re.sub(pattern, replacer, value)


def substitute_env_vars(value: Any) -> Any:
    if isinstance(value, str):
        return _substitute_env_vars_in_string(value)
    if isinstance(value, dict):
        return {key: substitute_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_env_vars(item) for item in value]
    return value


def parse_bool_env(value: str) -> bool:
    """An explicit allowlist serves YAML and environment flags; unknown and
    empty values are false.
    """
    return value.lower() in ("true", "1", "yes", "on")


def parse_timeout_env(env_var: str, default: float) -> float | None:
    """LLM and MCP disable/fallback rules share this parser; operators can
    retune without restarting.
    """
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return default
    try:
        seconds = float(raw)
    except ValueError:
        logger.warning(
            "ignoring non-numeric %s=%r; using default %ss",
            env_var,
            raw,
            default,
        )
        return default
    return seconds if seconds > 0 else None


def parse_list_env(env_var: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """An explicit empty value disables the preference, unlike an unset
    value.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())
