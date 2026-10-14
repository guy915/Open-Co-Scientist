import json
import logging
import warnings
from dataclasses import dataclass
from typing import Any, Final

from co_scientist.core.env_vars import parse_timeout_env
from co_scientist.platform.llm.admission.free_policy import (
    api_key_for_model,
    current_api_key,
)
from co_scientist.platform.llm.profile import model_profile
from co_scientist.platform.llm.request.backend import (
    active_backend,
    litellm_supports_json_schema,
)
from co_scientist.platform.llm.request.thinking import _apply_thinking_args
from co_scientist.platform.llm.request.transport import complete_request

logger = logging.getLogger(__name__)

# Reasoning can legitimately take minutes; bound wedged calls without policing
# ordinary latency.
LLM_TIMEOUT_ENV = "COSCIENTIST_LLM_TIMEOUT_SECONDS"
DEFAULT_LLM_TIMEOUT_SECONDS = 600.0


# Keep the answer-deliverable instruction ahead of the schema; position is the
# measured effect.
_ANSWER_DISCIPLINE: Final = (
    "\n\n## Answer Discipline\n\n"
    "Your reasoning is not your answer. When you have finished reasoning, "
    "you must write the JSON object described below as the content of your "
    "reply. A reply whose content is empty is discarded in full, however "
    "good the reasoning behind it was, so never end your turn without "
    "emitting the JSON."
)


def _inject_schema_into_prompt(prompt: str, json_schema: dict[str, Any]) -> str:
    """
    The deliverable instruction must precede the schema; moving it lost the fix.
    Without server enforcement, closed-field guidance is essential.
    """
    actual_schema = json_schema.get("schema", json_schema)
    schema_str = json.dumps(actual_schema, indent=2)
    return (
        prompt + _ANSWER_DISCIPLINE + "\n\n---\nRESPOND WITH VALID JSON ONLY. "
        "Your output MUST strictly match this JSON schema "
        "(all required fields must be present):\n" + schema_str + "\n\n"
        "Output a JSON object that CONFORMS TO the schema above -- the "
        "actual data. Do NOT output the schema itself: your response must "
        'not contain "type", "properties", or "required" keys unless the '
        "schema declares them as data fields. Use only the property names "
        "the schema lists; any field it does not declare will be rejected."
    )


def _apply_schema_response_format(
    completion_args: dict[str, Any],
    prompt: str,
    model_name: str,
    json_schema: dict[str, Any],
) -> None:
    # Ask the active backend per call; a from-import would freeze the default
    # capability answer.
    if active_backend().supports_json_schema(model_name):
        completion_args["response_format"] = {
            "type": "json_schema",
            # Callers use both bare schemas and provider envelopes.
            "json_schema": (
                {"name": "response", **json_schema}
                if "schema" in json_schema
                else {"name": "response", "schema": json_schema}
            ),
        }
        return

    logger.debug(
        "model %s does not support json_schema response format;"
        " downgrading to json_object with schema in prompt",
        model_name,
    )
    shimmed_content = _inject_schema_into_prompt(prompt, json_schema)
    completion_args["messages"] = [{"role": "user", "content": shimmed_content}]
    if model_profile(model_name).json_object:
        completion_args["response_format"] = {"type": "json_object"}


def _apply_response_format(
    completion_args: dict[str, Any],
    prompt: str,
    model_name: str,
    force_json: bool,
    json_schema: dict[str, Any] | None,
) -> None:
    if json_schema:
        _apply_schema_response_format(completion_args, prompt, model_name, json_schema)
    elif force_json and model_profile(model_name).json_object:
        completion_args["response_format"] = {"type": "json_object"}


def llm_timeout_seconds() -> float | None:
    """Read each call so operators can retune the ceiling without a restart."""
    return parse_timeout_env(LLM_TIMEOUT_ENV, DEFAULT_LLM_TIMEOUT_SECONDS)


def _apply_timeout(completion_args: dict[str, Any]) -> None:
    """Provider timeout kwargs are not universally forwarded; the await also
    needs a hard ceiling.
    """
    timeout = llm_timeout_seconds()
    if timeout is not None:
        completion_args["timeout"] = timeout


# Let provider deadlines fail first with endpoint-specific detail; the outer
# ceiling catches transport hangs.
_TIMEOUT_GRACE_SECONDS = 30.0


async def _acompletion_within_timeout(completion_args: dict[str, Any], model_name: str) -> Any:
    """Telemetry here counts physical attempts only."""
    return await complete_request(
        completion_args,
        model_name,
        byok=bool(current_api_key()),
        timeout_seconds=llm_timeout_seconds(),
        timeout_grace_seconds=_TIMEOUT_GRACE_SECONDS,
    )


# LiteLLM streaming/non-streaming Pydantic field mismatches produce
# serialization warnings.
warnings.filterwarnings("ignore", message=r".*Pydantic serializer warnings.*", category=UserWarning)


def _clamp_temperature(model_name: str, temperature: float) -> float:
    """Gemini 3 requires temperature >= 1.0 to avoid degraded performance."""
    floor = model_profile(model_name).min_temperature
    if floor is not None and temperature < floor:
        logger.debug(
            "clamping temperature %s -> %s for %s (it requires temp >= %s "
            "to avoid degraded performance)",
            temperature,
            floor,
            model_name,
            floor,
        )
        return floor
    return temperature


# Validation binds the default backend capability alias at import; tests also
# clear its cache.
_supports_json_schema_response_format = litellm_supports_json_schema


def _base_completion_args(
    prompt: str, model_name: str, max_tokens: int, temperature: float
) -> dict[str, Any]:
    completion_args: dict[str, Any] = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Providers support different parameter sets; unsupported optional
        # arguments must not reject the call.
        "drop_params": True,
    }

    _apply_timeout(completion_args)

    return completion_args


def _apply_api_key(completion_args: dict[str, Any]) -> None:
    """Omitting an absent key preserves provider environment resolution
    without shared mutation.
    """
    api_key = api_key_for_model(completion_args["model"])
    if api_key:
        completion_args["api_key"] = api_key


@dataclass(frozen=True)
class CompletionShape:
    force_json: bool = False
    json_schema: dict[str, Any] | None = None
    enable_thinking: bool = True


def _build_completion_args(
    prompt: str,
    model_name: str,
    max_tokens: int,
    temperature: float,
    shape: CompletionShape,
) -> dict[str, Any]:
    """BYOK travels in task context, keeping request arguments
    credential-free.
    """
    completion_args = _base_completion_args(prompt, model_name, max_tokens, temperature)

    _apply_response_format(completion_args, prompt, model_name, shape.force_json, shape.json_schema)

    _apply_thinking_args(completion_args, model_name, shape.enable_thinking)

    return completion_args
