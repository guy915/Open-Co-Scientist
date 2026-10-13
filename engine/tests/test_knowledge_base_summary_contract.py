from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema.exceptions import ValidationError

from co_scientist.platform.llm import CompletionSpec, call_llm_json
from co_scientist.platform.llm.structured.validate import validate_json_schema
from co_scientist.science.schemas.synthesis import (
    KNOWLEDGE_BASE_THEME_SCHEMA,
    RESEARCH_OVERVIEW_SCHEMA,
)
from tests._llm_fake import SCHEMA_FEEDBACK, make_completion, make_message, scripted_backend


@pytest.fixture(params=[KNOWLEDGE_BASE_THEME_SCHEMA, RESEARCH_OVERVIEW_SCHEMA])
def summary_schema(request: pytest.FixtureRequest) -> dict[str, Any]:
    fields = request.param["schema"]["properties"]
    schema: dict[str, Any] = fields.get("sections", fields.get("knowledge_base"))["items"][
        "properties"
    ]["summary"]
    return schema


@pytest.mark.parametrize(
    "summary",
    [
        "EGFR blockade suppresses ERK. Clinical benefit remains unproven.",
        " ".join(["word"] * 31) + ".",
        "",
        "   ",
        " A finding holds.",
        "A finding holds. ",
        "A finding holds\nwith a caveat.",
        "S. aureus biofilm tolerance may depend on metabolic state.",
    ],
)
def test_generated_summary_rejects_multiple_sentences_or_more_than_thirty_words(
    summary_schema: dict[str, Any],
    summary: str,
) -> None:
    with pytest.raises(ValidationError):
        validate_json_schema(
            {"summary": summary}, {"type": "object", "properties": {"summary": summary_schema}}
        )


@pytest.mark.parametrize(
    "summary",
    [
        "EGFR amplification occurs in 57.4% of cases, but clinical benefit remains unproven.",
        "Staphylococcus aureus biofilm tolerance may depend on metabolic state.",
        "The p.R132H variant is associated with altered enzyme activity.",
        " ".join(["word"] * 30) + ".",
    ],
)
def test_generated_summary_accepts_one_short_sentence_with_scientific_notation(
    summary_schema: dict[str, Any],
    summary: str,
) -> None:
    validate_json_schema(
        {"summary": summary}, {"type": "object", "properties": {"summary": summary_schema}}
    )


async def test_invalid_summary_is_regenerated_with_feedback_without_truncating_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section = {
        "heading": "EGFR signalling",
        "detail": "Full detail, including limitations.",
        "evidence_ids": ["ev-1"],
    }
    good = {
        "sections": [
            {
                **section,
                "summary": "EGFR blockade suppresses ERK, but clinical benefit remains unproven.",
            }
        ]
    }
    bad = {
        "sections": [
            {
                **section,
                "summary": "EGFR blockade suppresses ERK. Clinical benefit remains unproven.",
            }
        ]
    }
    fake = scripted_backend(
        monkeypatch, [make_completion(make_message(json.dumps(value))) for value in (bad, good)]
    )
    result = await call_llm_json(
        "Write the Knowledge Base.",
        CompletionSpec(
            model_name="test/model", max_tokens=1024, json_schema=KNOWLEDGE_BASE_THEME_SCHEMA
        ),
        max_attempts=2,
    )
    assert result == good
    assert len(fake.requests) == 2
    assert SCHEMA_FEEDBACK in fake.requests[1]["messages"][0]["content"]
