from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HypothesisField:
    engine: str
    store: str
    screened: bool
    redacted: bool
    engine_order: int
    store_order: int


HYPOTHESIS_FIELDS = (
    HypothesisField("text", "statement", True, False, 0, 0),
    HypothesisField("literature_grounding", "mechanism", True, True, 2, 1),
    HypothesisField("explanation", "expected_effect", True, True, 1, 2),
    HypothesisField("experiment", "experimental_context", True, True, 3, 3),
)


def store_text_fields(raw: Mapping[str, Any]) -> dict[str, str]:
    return {field.store: str(raw.get(field.engine) or "") for field in HYPOTHESIS_FIELDS}
