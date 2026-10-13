import copy
import json
import logging
import re
from collections.abc import Callable
from typing import Any, Literal, NoReturn, overload

import jsonschema
from jsonschema.exceptions import ValidationError

from co_scientist.core.exceptions import ResponseParseError
from co_scientist.platform.telemetry.progress import record_schema_degradation

logger = logging.getLogger(__name__)


ElementKind = Literal["any", "dict", "str"]


def _element_ok(item: Any, element: ElementKind) -> bool:
    if element == "dict":
        return isinstance(item, dict)
    if element == "str":
        return isinstance(item, str)
    return True


def _filter_elements(items: list[Any], element: ElementKind) -> tuple[list[Any], bool]:
    kept: list[Any] = []
    dropped = False
    for item in items:
        if not _element_ok(item, element):
            dropped = True
            continue
        if element == "str":
            cleaned = item.strip()
            if not cleaned:
                dropped = True
                continue
            kept.append(cleaned)
        else:
            kept.append(item)
    return kept, dropped


def _list_from_dict(
    value: dict[str, Any], keys: tuple[str, ...], element: ElementKind
) -> list[Any] | None:
    for key in keys:
        found = value.get(key)
        if isinstance(found, list):
            return found
    if element == "dict":
        return [value]
    return None


def _coerce_from_list(value: list[Any], element: ElementKind, site: str) -> list[Any]:
    items, dropped = _filter_elements(value, element)
    if dropped:
        logger.warning("%s: dropped list element(s) of the wrong type", site)
    return items


def _coerce_from_dict(
    value: dict[str, Any],
    keys: tuple[str, ...],
    element: ElementKind,
    site: str,
) -> list[Any]:
    found = _list_from_dict(value, keys, element)
    if found is None:
        logger.warning("%s: expected a list, got a dict with no usable list", site)
        return []
    items, _dropped = _filter_elements(found, element)
    logger.warning("%s: coerced a dict into a list", site)
    return items


def _coerce_from_scalar(value: Any, element: ElementKind, site: str) -> list[Any]:
    if element != "str" or not isinstance(value, str) or not value.strip():
        logger.warning("%s: expected a list, got %s", site, type(value).__name__)
        return []
    logger.warning("%s: coerced a bare value into a one-item list", site)
    return [value.strip()]


@overload
def coerce_json_list(
    value: Any,
    *,
    keys: tuple[str, ...] = (),
    element: Literal["str"],
    site: str,
) -> list[str]: ...


@overload
def coerce_json_list(
    value: Any,
    *,
    keys: tuple[str, ...] = (),
    element: Literal["dict"],
    site: str,
) -> list[dict[str, Any]]: ...


@overload
def coerce_json_list(
    value: Any,
    *,
    keys: tuple[str, ...] = (),
    element: Literal["any"] = "any",
    site: str,
) -> list[Any]: ...


def coerce_json_list(
    value: Any,
    *,
    keys: tuple[str, ...] = (),
    element: ElementKind = "any",
    site: str,
) -> list[Any]:
    if isinstance(value, list):
        return _coerce_from_list(value, element, site)
    if value is None:
        return []
    if isinstance(value, dict):
        return _coerce_from_dict(value, keys, element, site)
    return _coerce_from_scalar(value, element, site)


def extract_response_json(raw: str) -> str:
    text = raw.strip()
    lower = text.lower()
    if "```json" in lower:
        start = lower.find("```json") + 7
        end = text.find("```", start)
        text = text[start:] if end == -1 else text[start:end]
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        text = text[start:] if end == -1 else text[start:end]
    return text.strip()


def _repair_string_after_colon_or_comma(s: str, stripped: str) -> str | None:
    if re.search(r'[:,]\s*"[^"]*$', stripped):
        logger.debug("repaired: unterminated string after colon/comma")
        return s + '"'
    return None


def _repair_unterminated_field_name(s: str, stripped: str) -> str | None:
    """A matching shape commits even when no quote is needed; never fall
    through to array repair.
    """
    if not re.search(r'"\w+$', stripped):
        return None
    before_partial = stripped[:-20] if len(stripped) > 20 else ""
    if before_partial.count('"') % 2 == 1:
        logger.debug("repaired: unterminated field name/string")
        return s + '"'
    return s


def _looks_like_truncated_array_entry(stripped: str) -> bool:
    return stripped.endswith(",") or (stripped[-1].isalnum() and "[" in stripped)


def _repair_unterminated_array_string(s: str, stripped: str) -> str | None:
    if not _looks_like_truncated_array_entry(stripped):
        return None
    last_open_bracket = stripped.rfind("[")
    last_close_bracket = stripped.rfind("]")
    if last_open_bracket <= last_close_bracket:
        return s
    after_bracket = stripped[last_open_bracket:]
    if after_bracket.count('"') % 2 == 1:
        logger.debug("repaired: unterminated string in array")
        return s + '"'
    return s


_UNTERMINATED_STRING_REPAIRS: tuple[Callable[[str, str], str | None], ...] = (
    _repair_string_after_colon_or_comma,
    _repair_unterminated_field_name,
    _repair_unterminated_array_string,
)


def _repair_unterminated_string(s: str, stripped: str) -> str:
    for repair in _UNTERMINATED_STRING_REPAIRS:
        result = repair(s, stripped)
        if result is not None:
            return result
    return s


def _close_truncated_json(s: str) -> str:
    open_braces = s.count("{") - s.count("}")
    open_brackets = s.count("[") - s.count("]")

    stripped = s.rstrip()

    if not stripped:
        return s

    s = _repair_unterminated_string(s, stripped)

    s = re.sub(r",\s*$", "", s)

    result = s + ("]" * open_brackets) + ("}" * open_braces)

    if open_braces > 0 or open_brackets > 0:
        logger.debug("repaired: added %s ']' and %s '}'", open_brackets, open_braces)

    return result


def _fix_invalid_escapes(s: str) -> str:
    """Models put LaTeX in JSON strings; lone backslashes must survive as
    literal text.
    """
    return re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", s)


_MINOR_JSON_REPAIR_STRATEGIES: list[Callable[[str], dict[str, Any] | None]] = [
    lambda s: json.loads(re.sub(r",(\s*[}\]])", r"\1", s)),
    lambda s: json.loads(_fix_invalid_escapes(s)),
    lambda s: json.loads(_fix_invalid_escapes(re.sub(r",(\s*[}\]])", r"\1", s))),
    # strict=False admits literal control characters only, preserving complete
    # prose-valued JSON.
    lambda s: json.loads(s, strict=False),
    lambda s: json.loads(_fix_invalid_escapes(re.sub(r",(\s*[}\]])", r"\1", s)), strict=False),
]

# Truncation-oriented repairs belong only on the final attempt.
_MAJOR_JSON_REPAIR_STRATEGIES: list[Callable[[str], dict[str, Any] | None]] = [
    lambda s: json.loads(_close_truncated_json(s)),
    lambda s: json.loads(_close_truncated_json(re.sub(r",(\s*[}\]])", r"\1", s))),
    lambda s: json.loads(_close_truncated_json(re.sub(r',?\s*"[^"]*$', "", s))),
    lambda s: json.loads(_close_truncated_json(re.sub(r'[:,]\s*"[^"]*$', "", s))),
    lambda s: json.loads(_close_truncated_json(s[: s.rfind(",") + 1] if "," in s else s)),
    lambda s: json.loads(m.group(0)) if (m := re.search(r"\{.*\}", s, re.DOTALL)) else None,
]


def _try_minor_repairs(json_str: str) -> dict[str, Any] | None:
    for i, repair_fn in enumerate(_MINOR_JSON_REPAIR_STRATEGIES):
        try:
            result = repair_fn(json_str)
            if result:
                logger.debug("JSON repaired using minor repair strategy %s", i)
                return result
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.debug("minor repair strategy %s failed: %s", i, e)
    return None


def _try_major_repairs(json_str: str) -> dict[str, Any] | None:
    for i, repair_fn in enumerate(_MAJOR_JSON_REPAIR_STRATEGIES):
        try:
            result = repair_fn(json_str)
            if result:
                # The caller identifies the repaired phase; warnings here
                # duplicate that event.
                logger.debug(
                    "JSON repaired using major repair strategy %s "
                    "(indicates truncation/incomplete response)",
                    i,
                )
                return result
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            if i < 2:
                logger.debug("major repair strategy %s failed: %s", i, e)
    return None


def _try_direct_parse(json_str: str) -> dict[str, Any] | None:
    try:
        result = json.loads(json_str)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def attempt_json_repair(
    json_str: str, allow_major_repairs: bool = False
) -> tuple[dict[str, Any] | None, bool]:
    direct_result = _try_direct_parse(json_str)
    if direct_result is not None:
        return direct_result, False

    minor_result = _try_minor_repairs(json_str)
    if minor_result is not None:
        return minor_result, False

    if allow_major_repairs:
        major_result = _try_major_repairs(json_str)
        if major_result is not None:
            return major_result, True

    return None, False


def parse_tool_loop_json(final_response: str, list_key: str, phase_label: str) -> list[Any]:
    """Tool-loop final answers lack schema enforcement and may be truncated.
    Unparseable output must fail rather than silently erase the generation
    phase.
    """
    response_text = extract_response_json(final_response)
    response_data, was_repaired = attempt_json_repair(response_text, allow_major_repairs=True)

    if response_data is None:
        logger.error(
            "Failed to parse %s JSON response after all repair attempts (%s characters)",
            phase_label,
            len(final_response),
        )
        raise ResponseParseError(f"{phase_label} returned invalid JSON that could not be repaired")

    if was_repaired:
        logger.warning(
            "%s JSON response required major repairs (possible truncation)",
            phase_label,
        )

    return coerce_json_list(
        response_data.get(list_key),
        keys=(list_key, "items"),
        element="dict",
        site=phase_label,
    )


def validate_json_schema(result: dict[str, Any], json_schema: dict[str, Any] | None) -> None:
    if json_schema is None:
        return

    # Accept bare schemas and response-format wrappers.
    actual_schema = json_schema.get("schema", json_schema)

    try:
        jsonschema.validate(instance=result, schema=actual_schema)
        logger.debug("JSON schema validation passed")
    except ValidationError as e:
        # The retry boundary warns with the attempt number; warning here would
        # duplicate it.
        logger.debug("JSON schema validation failed (path depth=%s)", len(e.path))
        raise


# Optional enhancements may degrade; foundational generation/planning must fail
# loudly.
_ENHANCEMENT_NODE_FALLBACKS: dict[str, dict[str, Any]] = {
    "proximity_analysis": {
        "similarity_clusters": [],
        "diversity_assessment": "Analysis failed - skipping deduplication",
        "redundancy_assessment": "Analysis failed - skipping deduplication",
    },
    "hypothesis_evolution": {},
    "hypothesis_review": {},
    "hypothesis_batch_review": {"reviews": []},
    "reflection_observations": {},
    "meta_review": {},
    "deep_verification": {},
    "research_overview": {},
}


def get_fallback_response(
    json_schema: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if json_schema is None:
        return None

    schema_name = json_schema.get("name")
    if not isinstance(schema_name, str):
        return None
    fallback = _ENHANCEMENT_NODE_FALLBACKS.get(schema_name)
    if fallback is not None:
        logger.warning(
            "Node '%s' failed JSON parsing after all retries; returning a "
            "fallback so the run degrades gracefully instead of aborting.",
            schema_name,
        )
        # Make degraded sections durable and visible rather than unexplained
        # blank output.
        record_schema_degradation(schema_name)
        # Deep-copy fallback templates so one caller cannot corrupt later calls.
        return copy.deepcopy(fallback)

    return None


def _default_for_field_schema(schema: dict[str, Any]) -> Any:
    kind = schema.get("type")
    if kind == "string":
        return schema["enum"][0] if "enum" in schema else ""
    factories = {"object": dict, "array": list, "integer": int, "number": int}
    factory = factories.get(kind) if isinstance(kind, str) else None
    return factory() if factory is not None else ""


def reshape_json_output(obj: Any, schema: Any, _path: str = "") -> None:
    """Only unenforced schemas are reshaped; wrong types still reach normal
    validation.
    """
    if not isinstance(obj, dict) or not isinstance(schema, dict):
        return
    props = schema.get("properties", {})
    _reshape_object_fields(obj, schema, props)
    for key, value in obj.items():
        if key in props:
            field_path = f"{_path}.{key}" if _path else key
            obj[key] = _reshape_value(value, props[key], field_path)


def _reshape_object_fields(
    obj: dict[str, Any], schema: dict[str, Any], props: dict[str, Any]
) -> None:
    if schema.get("additionalProperties") is False:
        for key in obj.keys() - props.keys():
            del obj[key]
    for field in schema.get("required", []):
        if field not in obj and field in props:
            obj[field] = _default_for_field_schema(props[field])


def _reshape_value(value: Any, schema: Any, field_path: str) -> Any:
    if not isinstance(schema, dict):
        return value
    if schema.get("type") == "array" and isinstance(value, dict) and len(value) == 1:
        enclosed = next(iter(value.values()))
        if isinstance(enclosed, list):
            value = enclosed
    if isinstance(value, str):
        return _truncate_string_value(value, schema.get("maxLength"), field_path)
    if isinstance(value, list):
        _reshape_array(value, schema, field_path)
        return value
    reshape_json_output(value, schema, field_path)
    return value


def _reshape_array(value: list[Any], schema: dict[str, Any], field_path: str) -> None:
    limit = schema.get("maxItems")
    if isinstance(limit, int) and len(value) > limit:
        del value[limit:]
    item_schema = schema.get("items")
    for index, item in enumerate(value):
        value[index] = _reshape_value(item, item_schema, f"{field_path}[{index}]")


def _truncate_string_value(value: str, max_length: Any, field_path: str) -> str:
    if not isinstance(max_length, int) or len(value) <= max_length:
        return value
    candidate = value[:max_length]
    boundary_window = max_length - max(1, round(max_length * 0.2))
    boundary = candidate.rfind(" ", max(0, boundary_window))
    truncated = candidate[:boundary].rstrip() if boundary != -1 else candidate
    logger.warning(
        "Truncated over-long string at '%s': %d chars -> %d chars (schema maxLength=%d)",
        field_path,
        len(value),
        len(truncated),
        max_length,
    )
    return truncated


def _validation_feedback(error: ValidationError) -> str:
    error_path = ".".join(str(p) for p in error.path) if error.path else "root"
    return (
        "\n\n--- VALIDATION ERROR FROM PREVIOUS"
        " ATTEMPT ---\n"
        f"Error: {error.message}\n"
        f"Location: {error_path}\n"
        "Please ensure your JSON output strictly"
        " matches the required schema structure.\n"
        "---"
    )


def _log_json_parse_failure_diagnostics(last_response_text: str) -> None:
    logger.error("Failed to parse JSON response after all repair attempts.")
    logger.error("Response length: %s chars", len(last_response_text))
    try:
        json.loads(last_response_text)
    except json.JSONDecodeError as error:
        logger.error("JSON error at position %s: %s", error.pos, error.msg)


def _raise_validation_error(last_error: ValidationError, max_attempts: int) -> NoReturn:
    raise ValidationError(
        f"Schema validation failed after {max_attempts} attempts: {last_error.message}",
        instance=last_error.instance,
        schema=last_error.schema,
        schema_path=last_error.schema_path,
        path=last_error.path,
    )


def _json_decode_error_pos(last_error: Exception | None) -> int:
    if isinstance(last_error, json.JSONDecodeError):
        return last_error.pos
    return 0


def _raise_json_decode_error(
    last_error: Exception | None,
    last_response_text: str | None,
    max_attempts: int,
) -> NoReturn:
    raise json.JSONDecodeError(
        f"Could not parse LLM response as JSON after {max_attempts} attempts",
        last_response_text or "",
        _json_decode_error_pos(last_error),
    )


def _raise_json_parse_error(
    last_error: Exception | None,
    last_response_text: str | None,
    max_attempts: int,
) -> NoReturn:
    if isinstance(last_error, ValidationError):
        _raise_validation_error(last_error, max_attempts)
    _raise_json_decode_error(last_error, last_response_text, max_attempts)


def _handle_json_retries_exhausted(
    json_schema: dict[str, Any] | None,
    last_error: Exception | None,
    last_response_text: str | None,
    max_attempts: int,
) -> dict[str, Any]:
    fallback = get_fallback_response(json_schema)
    if fallback is not None:
        logger.warning("Returning fallback data for non-critical node after all retries exhausted")
        return fallback

    if last_response_text:
        _log_json_parse_failure_diagnostics(last_response_text)

    _raise_json_parse_error(last_error, last_response_text, max_attempts)
