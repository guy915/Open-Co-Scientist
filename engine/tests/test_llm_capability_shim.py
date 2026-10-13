from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest

from co_scientist.platform.llm import CompletionSpec, call_llm
from co_scientist.platform.llm.request.backend import _registry_supports_json_schema
from co_scientist.platform.llm.structured.validate import reshape_json_output
from co_scientist.science.schemas.review import FULL_REVIEW_SCHEMA
from tests._llm_fake import NESTED_SCHEMA, scripted_backend
from tests._llm_fake import make_completion as _completion
from tests._llm_fake import make_message as _message

pytestmark = pytest.mark.usefixtures("clear_capability_cache")

_ANSWER_DISCIPLINE = (
    "\n\n## Answer Discipline\n\n"
    "Your reasoning is not your answer. When you have finished reasoning, "
    "you must write the JSON object described below as the content of your "
    "reply. A reply whose content is empty is discarded in full, however "
    "good the reasoning behind it was, so never end your turn without "
    "emitting the JSON."
)

_SCHEMA_PROMPT_SUFFIX = _ANSWER_DISCIPLINE + (
    "\n\n---\nRESPOND WITH VALID JSON ONLY. "
    "Your output MUST strictly match this JSON schema "
    "(all required fields must be present):\n"
)

_SCHEMA_PROMPT_TRAILER = (
    "\n\n"
    "Output a JSON object that CONFORMS TO the schema above -- the "
    "actual data. Do NOT output the schema itself: your response must "
    'not contain "type", "properties", or "required" keys unless the '
    "schema declares them as data fields. Use only the property names "
    "the schema lists; any field it does not declare will be rejected."
)

_FLAT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _registry(monkeypatch: pytest.MonkeyPatch, supported: bool) -> None:
    _registry_supports_json_schema.cache_clear()
    monkeypatch.setattr(
        "co_scientist.platform.llm.litellm.supports_response_schema",
        lambda model: supported,
    )


def _serve(
    monkeypatch: pytest.MonkeyPatch, supported: bool, *contents: str
) -> list[dict[str, Any]]:
    _registry(monkeypatch, supported)
    return scripted_backend(monkeypatch, [_completion(_message(c)) for c in contents]).requests


@pytest.mark.parametrize("schema", [NESTED_SCHEMA, _FLAT_SCHEMA], ids=["named", "bare"])
async def test_an_unsupported_model_gets_json_object_mode_and_a_schema_prompt(
    monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any]
) -> None:
    sent = _serve(monkeypatch, False, "{}")

    await call_llm("a prompt", CompletionSpec(model_name="test-model", json_schema=schema))

    assert sent[0]["response_format"] == {"type": "json_object"}
    assert sent[0]["messages"] == [
        {
            "role": "user",
            "content": (
                "a prompt"
                + _SCHEMA_PROMPT_SUFFIX
                + json.dumps(schema.get("schema", schema), indent=2)
                + _SCHEMA_PROMPT_TRAILER
            ),
        }
    ]


def test_a_full_review_missing_its_summary_blocks_is_rescued() -> None:
    """Required nested review blocks must backfill on JSON-object
    providers."""
    schema = FULL_REVIEW_SCHEMA["schema"]
    answer: dict[str, Any] = {
        "correctness": "Internally consistent.",
        "assumptions": [],
        "quality_and_novelty": "A non-obvious combination.",
        "literature_grounding": "Two cohort studies agree.",
        "verdict": "sound",
        "justification": "Worth a pilot.",
    }

    reshape_json_output(answer, schema)

    jsonschema.validate(instance=answer, schema=schema)
    assert answer["reviews_summary"]["critical_flaws"] == []
    assert answer["feasibility_steps"] == []
