import json
import logging
from dataclasses import dataclass
from typing import Any

from jsonschema.exceptions import ValidationError

from co_scientist.platform.llm.admission.free_policy import scoped_api_key
from co_scientist.platform.llm.attempts.retry import Accepted, Attempt, Rejected
from co_scientist.platform.llm.precall import _prepare_llm_call

# Validation deliberately binds the default capability answer; request shaping
# asks the active backend.
from co_scientist.platform.llm.request.completion import (
    CompletionShape,
    _acompletion_within_timeout,
    _apply_api_key,
    _build_completion_args,
    _supports_json_schema_response_format,
)
from co_scientist.platform.llm.request.response import _extract_completion_content
from co_scientist.platform.llm.request.thinking import annotate_failure_context
from co_scientist.platform.llm.structured.validate import (
    _validation_feedback,
    attempt_json_repair,
    reshape_json_output,
    validate_json_schema,
)
from co_scientist.platform.llm.values import CompletionSpec, LLMCallOptions, LLMRequest

logger = logging.getLogger(__name__)


async def _call_llm_single_attempt(
    prompt: str,
    spec: CompletionSpec,
    opt: LLMCallOptions,
) -> str:
    """The request stays credential-free; an explicit key overrides this task
    context only for this call, after temperature clamping.
    """
    with scoped_api_key(spec.api_key):
        request = _prepare_llm_call(
            LLMRequest(
                prompt=prompt,
                model_name=spec.model_name,
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                json_schema=spec.json_schema,
                force_json=spec.force_json,
            )
        )
        try:
            completion_args = _build_completion_args(
                request.prompt,
                request.model_name,
                request.max_tokens,
                request.temperature,
                CompletionShape(
                    force_json=bool(request.force_json),
                    json_schema=request.json_schema,
                    enable_thinking=opt.enable_thinking,
                ),
            )
            _apply_api_key(completion_args)
            response = await _acompletion_within_timeout(completion_args, request.model_name)
            return _extract_completion_content(response, request.model_name)
        except Exception as e:
            # Report the actual sent budget; only the retry boundary knows
            # whether failure is terminal, so it owns the log.
            schema_name = (spec.json_schema or {}).get("name")
            annotate_failure_context(
                e,
                spec.model_name,
                spec.max_tokens,
                opt.enable_thinking,
                schema_name if isinstance(schema_name, str) else None,
            )
            raise


@dataclass(frozen=True)
class _ParsedResponse:
    result: dict[str, Any] | None
    repaired: bool
    parse_error: Exception | None


def _parse_or_repair_json(
    response_text: str,
    is_final_attempt: bool,
) -> _ParsedResponse:
    """Major repairs can hide truncation, so they are reserved for the final
    attempt.
    """
    result: dict[str, Any] | None = None
    parse_error: Exception | None = None
    try:
        result = json.loads(response_text)
        if not isinstance(result, dict):
            parse_error = ValueError("Parsed JSON is not a dictionary")
            result = None
    except json.JSONDecodeError as e:
        parse_error = e
        result = None

    repaired = False
    if result is None:
        result, _ = attempt_json_repair(response_text, allow_major_repairs=is_final_attempt)
        repaired = result is not None

    return _ParsedResponse(result, repaired, parse_error)


def _backfill_and_validate(
    result: dict[str, Any],
    json_schema: dict[str, Any],
    model_name: str,
) -> None:
    # Only downgraded responses need reshaping; native-schema anomalies remain
    # real validation failures.
    if not _supports_json_schema_response_format(model_name):
        schema = json_schema.get("schema", json_schema)
        reshape_json_output(result, schema)
    validate_json_schema(result, json_schema)


def _validation_failure(
    error: ValidationError,
    response_text: str,
    attempt: Attempt,
    repaired: bool,
) -> Rejected:
    logger.warning(
        "Schema validation failed%s on attempt %s: %s",
        " after repair" if repaired else "",
        attempt.number,
        error.message,
    )
    feedback = None if attempt.is_final else _validation_feedback(error)
    return Rejected(error, response_text, feedback)


@dataclass(frozen=True)
class JsonJudge:
    original_prompt: str
    spec: CompletionSpec

    def prompt_for(self, attempt: Attempt) -> str:
        return self.original_prompt + (attempt.feedback or "")

    def verdict(self, response_text: str, attempt: Attempt) -> Accepted[dict[str, Any]] | Rejected:
        """Validation and parse failures carry corrective feedback; genuine
        call failures bypass judging.
        """
        parsed = _parse_or_repair_json(response_text, attempt.is_final)
        if parsed.result is None:
            return Rejected(
                parsed.parse_error or ValueError("All repair strategies failed"),
                response_text,
            )
        return self._validated(parsed.result, parsed.repaired, response_text, attempt)

    def _validated(
        self,
        result: dict[str, Any],
        repaired: bool,
        response_text: str,
        attempt: Attempt,
    ) -> Accepted[dict[str, Any]] | Rejected:
        try:
            if self.spec.json_schema is not None:
                _backfill_and_validate(result, self.spec.json_schema, self.spec.model_name)
        except ValidationError as e:
            return _validation_failure(e, response_text, attempt, repaired)
        return Accepted(result)
