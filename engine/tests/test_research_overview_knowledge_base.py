from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from co_scientist.core.constants import (
    KNOWLEDGE_BASE_OUTLINE_MAX_TOKENS,
    KNOWLEDGE_BASE_THEME_MAX_TOKENS,
    MINIMAL_REASONING_MAX_TOKENS,
    THINKING_FLOOR_MAX_TOKENS,
)
from co_scientist.science.meta_review import research_overview as ro
from co_scientist.science.meta_review import (
    research_overview_knowledge_base as kb,
)
from co_scientist.science.meta_review import (
    research_overview_knowledge_base as kbc,
)
from co_scientist.science.meta_review import research_overview_review as ror
from tests._state import make_hypothesis, make_state
from tests.test_research_overview import (
    _RESEARCH_OVERVIEW_OVERVIEW_RESPONSE as _OVERVIEW_RESPONSE,
)
from tests.test_research_overview import (
    _research_overview_grounded_articles as _grounded_articles,
)


def _research_overview_knowledge_base_funded_state(**overrides: Any) -> Any:
    return make_state(
        research_goal="g",
        supervisor_model_name="test/model",
        budget={"max_iterations": 3, "max_llm_calls": 7000},
        **overrides,
    )


async def test_an_empty_corpus_never_spends_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = AsyncMock(return_value=_OUTLINE)
    monkeypatch.setattr(kbc, "call_llm_json", fake)

    result = await kb.synthesize_knowledge_base(
        _research_overview_knowledge_base_funded_state(),
        "1. (Elo 1200) an idea",
        {},
    )

    assert result == ([], 0)
    assert fake.await_count == 0


_ASKED = "Theme to write:"
"""The line naming which theme one writing call is responsible for."""

_CORPUS: dict[str, dict[str, Any]] = {
    "evidence-1": {
        "evidence_id": "evidence-1",
        "source_id": "PMID:1",
        "title": "Stellate cell plasticity",
        "abstract": "Quiescent stellate cells store retinoids.",
        "source": "pubmed",
        "url": "",
    },
    "evidence-2": {
        "evidence_id": "evidence-2",
        "source_id": "PMID:2",
        "title": "Matrix cross-linking",
        "abstract": "LOXL2 stabilises fibrillar collagen.",
        "source": "pubmed",
        "url": "",
    },
}

_OUTLINE: dict[str, Any] = {
    "themes": [
        {
            "title": "Hepatic Stellate Cell Plasticity",
            "sections": [
                {
                    "heading": "Quiescent And Activated States",
                    "evidence_ids": ["evidence-1"],
                },
                {
                    "heading": "Ungrounded Section",
                    "evidence_ids": ["invented"],
                },
            ],
        },
        {
            "title": "Extracellular Matrix Architecture",
            "sections": [
                {
                    "heading": "Cross-Linking Constraints",
                    "evidence_ids": ["evidence-2"],
                }
            ],
        },
    ]
}

_THEME_SECTIONS: dict[str, dict[str, Any]] = {
    "Hepatic Stellate Cell Plasticity": {
        "sections": [
            {
                "heading": "Quiescent And Activated States",
                "summary": (
                    "Quiescent stellate cells store retinoids; activation changes their role."
                ),
                "detail": "Quiescent cells store retinoids.",
                "evidence_ids": ["evidence-1"],
            },
            {
                "heading": "Ungrounded Section",
                "detail": "No source stands behind this.",
                "evidence_ids": ["invented"],
            },
        ]
    },
    "Extracellular Matrix Architecture": {
        "sections": [
            {
                "heading": "Cross-Linking Constraints",
                "detail": "LOXL2 raises the denaturation temperature.",
                "evidence_ids": ["evidence-2"],
            }
        ]
    },
}


class _Responder:
    def __init__(self, failing_theme: str | None = None) -> None:
        self.failing_theme = failing_theme
        self.specs: list[Any] = []
        self.options: list[Any] = []
        self.prompts: list[str] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        spec = kwargs["spec"]
        self.specs.append(spec)
        self.options.append(kwargs.get("options"))
        prompt = kwargs["prompt"]
        self.prompts.append(prompt)
        assert spec.json_schema is not None
        if spec.json_schema["name"] == "knowledge_base_outline":
            return _OUTLINE
        asked = next(line for line in prompt.splitlines() if line.startswith(_ASKED))
        theme = next(title for title in _THEME_SECTIONS if title in asked)
        if theme == self.failing_theme:
            raise RuntimeError("stream dropped")
        return _THEME_SECTIONS[theme]


async def _synthesize(
    monkeypatch: pytest.MonkeyPatch, responder: Any
) -> tuple[list[dict[str, Any]], int]:
    monkeypatch.setattr(kbc, "call_llm_json", responder)
    return await kb.synthesize_knowledge_base(
        _research_overview_knowledge_base_funded_state(),
        "1. (Elo 1200) an idea",
        _CORPUS,
    )


async def test_the_synthesis_is_one_bounded_outline_call_plus_one_per_theme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advertised output capacity can exceed what the per-call clock can serve,
    and unbounded reasoning can spend the clock without writing an answer."""
    responder = _Responder()

    topics, calls = await _synthesize(monkeypatch, responder)

    assert calls == 1 + len(_OUTLINE["themes"])
    assert len(responder.specs) == calls
    assert KNOWLEDGE_BASE_OUTLINE_MAX_TOKENS < KNOWLEDGE_BASE_THEME_MAX_TOKENS
    assert all(spec.max_tokens <= THINKING_FLOOR_MAX_TOKENS for spec in responder.specs)
    assert all(
        options is not None and options.reasoning_max_tokens == MINIMAL_REASONING_MAX_TOKENS
        for options in responder.options
    )
    assert [options.prompt_name for options in responder.options] == [
        "knowledge_base_outline",
        "knowledge_base_theme",
        "knowledge_base_theme",
    ]
    for prompt in responder.prompts[1:]:
        assert all(title in prompt for title in _THEME_SECTIONS)
    assert [topic["theme"] for topic in topics] == [
        "Hepatic Stellate Cell Plasticity",
        "Extracellular Matrix Architecture",
    ]
    assert [topic["title"] for topic in topics] == [
        "Quiescent And Activated States",
        "Cross-Linking Constraints",
    ]
    assert [topic["id"] for topic in topics] == ["topic-1", "topic-2"]
    assert topics[0]["references"][0]["title"] == "Stellate cell plasticity"
    assert topics[0]["summary"] == (
        "Quiescent stellate cells store retinoids; activation changes their role."
    )
    assert "Ungrounded Section" not in str(topics)


async def test_a_theme_that_does_not_answer_drops_only_its_own_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topics, calls = await _synthesize(
        monkeypatch,
        _Responder(failing_theme="Hepatic Stellate Cell Plasticity"),
    )

    assert calls == 1 + len(_OUTLINE["themes"])
    assert [topic["theme"] for topic in topics] == ["Extracellular Matrix Architecture"]


_DRAFT: dict[str, Any] = {
    "overview": {"summary": "Original summary.", "research_directions": []},
    "nih_specific_aims": {"disease_description": "i", "aims": []},
    "research_contacts": [],
    "knowledge_base": [],
}


def _revised(summary: str) -> dict[str, Any]:
    return {
        "overview": {"summary": summary, "research_directions": []},
        "nih_specific_aims": {"disease_description": "i", "aims": []},
        "research_contacts": [],
        "knowledge_base": [],
    }


def _reject(location: str = "overview.summary") -> dict[str, Any]:
    return {
        "accept": False,
        "notes": [{"location": location, "issue": "Unsupported claim."}],
    }


async def _run_loop(
    state: Any, draft: dict[str, Any] = _DRAFT
) -> tuple[dict[str, Any], dict[str, Any], int]:
    context = ror.OverviewReviewContext(
        state=state,
        research_goal="g",
        hypotheses_summary="1. (Elo 1600) h",
        contact_candidates_text="No verified literature authors available.",
        evidence_corpus_text="No verified evidence corpus available.",
    )
    return await ror.review_research_overview(context, draft)


async def test_the_cycle_cap_holds_when_the_reviewer_objects_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_revision = _revised("Second revision.")
    fake = AsyncMock(
        side_effect=[
            _reject("overview.summary"),
            _revised("First revision."),
            _reject("aims[0]"),
            second_revision,
        ]
    )
    monkeypatch.setattr(ror, "call_llm_json", fake)

    state = make_state(supervisor_model_name="test/model")
    final, meta, calls = await _run_loop(state)

    assert fake.await_count == 4
    assert calls == 4
    assert final["overview"]["summary"] == "Second revision."
    assert meta == {"reviewed": True, "rounds": 2}


"""Directions the canned draft names, each bought its own writing call."""


def _base_state(**overrides: Any) -> Any:
    h = make_hypothesis(text="HDAC inhibition reverses fibrosis", elo_rating=1700)
    return make_state(
        hypotheses=[h],
        research_goal="g",
        supervisor_model_name="test/model",
        meta_review={},
        articles=_grounded_articles(),
        **overrides,
    )


async def test_an_exception_in_the_review_loop_publishes_the_original_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review failure must degrade to the draft rather than prevent
    publication."""
    synth = AsyncMock(return_value=_OVERVIEW_RESPONSE)
    monkeypatch.setattr(ro, "call_llm_json", synth)
    loop = AsyncMock(side_effect=RuntimeError("provider exploded"))
    monkeypatch.setattr(ro, "review_research_overview", loop)

    out = await ro.research_overview_node(_base_state(enable_overview_review=True))

    loop.assert_awaited_once()
    assert out["research_overview"]["overview"]["summary"] == "S"
    assert out["research_overview"]["overview_review"] == {
        "reviewed": False,
        "rounds": 0,
    }
    assert out["metrics"].llm_calls == 2
