from collections.abc import Mapping
from typing import Any, Literal

from co_scientist.domains.research_state.hypothesis_fields import HYPOTHESIS_FIELDS

REDACTED_PLACEHOLDER = "[REDACTED FOR SAFETY]"
Namespace = Literal["engine", "store"]


def screened_text(
    hypothesis: Mapping[str, Any], namespace: Namespace, *, separator: str = "\n"
) -> str:
    # Regex windows can span fields, so each namespace retains its field order.
    ordered = sorted(
        HYPOTHESIS_FIELDS,
        key=lambda field: field.engine_order if namespace == "engine" else field.store_order,
    )
    parts = [
        str(hypothesis.get(field.engine if namespace == "engine" else field.store) or "")
        for field in ordered
        if field.screened
    ]
    return separator.join(part for part in parts if part)


def redactable_fields(namespace: Namespace) -> tuple[str, ...]:
    return tuple(
        field.engine if namespace == "engine" else field.store
        for field in HYPOTHESIS_FIELDS
        if field.redacted
    )


def redact_value(name: str, value: str | None) -> str | None:
    sensitive = any(
        name in (field.engine, field.store) and field.redacted for field in HYPOTHESIS_FIELDS
    )
    return REDACTED_PLACEHOLDER if sensitive and value else value
