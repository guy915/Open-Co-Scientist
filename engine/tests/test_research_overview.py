from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from co_scientist.core.constants import (
    RESEARCH_OVERVIEW_DIRECTION_MAX_TOKENS,
    RESEARCH_OVERVIEW_INTERIM_MAX_TOKENS,
    RESEARCH_OVERVIEW_MAX_TOKENS,
)
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.platform.retrieval.article import Article
from co_scientist.science.meta_review import research_overview as ro
from co_scientist.science.meta_review import (
    research_overview_direction_calls as calls,
)
from co_scientist.science.scheduling import TaskType
from co_scientist.science.schemas.synthesis import (
    RESEARCH_OVERVIEW_MAX_DIRECTIONS,
)
from tests._state import make_article, make_hypothesis, make_state

_RESEARCH_OVERVIEW_OVERVIEW_RESPONSE: dict[str, Any] = {
    "overview": {
        "summary": "S",
        "research_directions": [
            {
                "title": "T",
                "importance": "I",
                "suggested_experiments": ["E"],
            }
        ],
    },
    "nih_specific_aims": {
        "disease_description": "intro",
        "aims": [
            {
                "overarching_goal": "A",
                "hypothesis": "R",
                "reasoning": "Ap",
            }
        ],
        "impact": "imp",
    },
    "research_contacts": [
        {
            "candidate_id": "author-1-1",
            "name": "Ada Researcher",
            "expertise": "Fibrosis mechanisms",
            "justification": "Authored the analyzed source.",
            "research_direction": "Epigenetic control of fibrosis",
        },
        {
            "candidate_id": "invented",
            "name": "Invented Person",
            "expertise": "Unknown",
            "justification": "Not grounded.",
        },
    ],
    "knowledge_base": [
        {
            "title": "Epigenetic control of fibrosis",
            "summary": "HDAC activity is implicated in fibrosis.",
            "detail": "The analyzed study supports a testable axis.",
            "uncertainty": "Causality remains unresolved.",
            "evidence_ids": ["evidence-1", "invented-evidence"],
        },
        {
            "title": "Unsupported topic",
            "summary": "No source.",
            "detail": "No source.",
            "uncertainty": "Unknown.",
            "evidence_ids": ["invented-evidence"],
        },
    ],
}


def _research_overview_grounded_articles() -> list[Article]:
    return [
        make_article(
            title="Fibrosis mechanisms",
            authors=["Ada Researcher"],
            source_id="PMID:123",
            url="https://pubmed.ncbi.nlm.nih.gov/123/",
            used_in_analysis=True,
        )
    ]


_BLOCKED_HYPOTHESIS_FIELDS: list[tuple[str, str]] = [
    ("review_disposition", "inaccurate"),
    ("review_disposition", "non_novel"),
    ("review_disposition", "inaccurate_and_non_novel"),
    ("review_disposition", "evidence_blocked"),
    ("safety_status", "prohibited"),
    ("safety_status", "ethical_concern"),
    ("safety_status", "uncertain"),
]


def _blocked_hypotheses() -> list[Hypothesis]:
    """Blocked ideas outrank healthy ones to expose filtering after top-k
    selection."""
    return [
        make_hypothesis(
            text=f"blocked idea {index} ({field_name}={value})",
            elo_rating=2000 + index,
            **{field_name: value},
        )
        for index, (field_name, value) in enumerate(_BLOCKED_HYPOTHESIS_FIELDS)
    ]


async def _first_prompt(
    monkeypatch: pytest.MonkeyPatch, hypotheses: list[Hypothesis]
) -> tuple[str, AsyncMock]:
    fake = AsyncMock(return_value=_RESEARCH_OVERVIEW_OVERVIEW_RESPONSE)
    monkeypatch.setattr(ro, "call_llm_json", fake)
    await ro.research_overview_node(
        make_state(
            hypotheses=hypotheses,
            research_goal="g",
            supervisor_model_name="test/model",
            meta_review={},
        )
    )
    return fake.await_args_list[0].kwargs["prompt"], fake


async def test_publication_gates_filter_before_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prose synthesized from a blocked idea cannot be safely unlabeled
    later."""
    blocked = _blocked_hypotheses()
    needs_revision = make_hypothesis(
        text="needs revision idea",
        elo_rating=1400,
        review_disposition="needs_revision",
    )
    healthy = make_hypothesis(text="healthy idea", elo_rating=1500)

    prompt, _ = await _first_prompt(monkeypatch, [healthy, needs_revision, *blocked])

    assert "healthy idea" in prompt
    # needs_revision ranks and publishes; only the not-viable band blocks.
    assert "needs revision idea" in prompt
    assert not any(hypothesis.text in prompt for hypothesis in blocked)


async def test_all_blocked_pool_skips_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = AsyncMock(return_value=_RESEARCH_OVERVIEW_OVERVIEW_RESPONSE)
    monkeypatch.setattr(ro, "call_llm_json", fake)

    out = await ro.research_overview_node(
        make_state(
            hypotheses=_blocked_hypotheses(),
            research_goal="g",
            supervisor_model_name="test/model",
            meta_review={},
        )
    )

    assert out == {"research_overview": {}}
    assert fake.await_count == 0


_RESPONSE: dict[str, Any] = {
    "overview": {
        "summary": "Where the evidence points.",
        "research_directions": [
            {
                "title": "Releasing the myeloid brake",
                "importance": "It unlocks fibrolysis.",
                "suggested_experiments": ["Block LILRB4."],
            }
        ],
    },
    "nih_specific_aims": {},
    "open_questions": ["What sets the reversal threshold?"],
    "knowledge_base": [],
}


def _state(**overrides: Any) -> Any:
    return make_state(
        hypotheses=[make_hypothesis(text="an idea", elo_rating=1700)],
        research_goal="g",
        supervisor_model_name="test/model",
        meta_review={},
        articles=[make_article(used_in_analysis=True)],
        enable_overview_review=True,
        budget={"max_iterations": 3, "max_llm_calls": 7000},
        **overrides,
    )


def test_format_overview_normalizes_null_research_directions() -> None:
    assert calls.format_overview(
        {"summary": "Keep this synthesis.", "research_directions": None}
    ) == {"summary": "Keep this synthesis.", "research_directions": []}


async def test_a_periodic_firing_writes_only_a_lean_interim_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Periodic calls must not buy terminal prose, review or a deep knowledge
    base that their consumers discard."""
    fake = AsyncMock(return_value=_RESPONSE)
    monkeypatch.setattr(ro, "call_llm_json", fake)

    out = await ro.research_overview_node(_state(next_task=TaskType.SYNTHESIZE.value))

    assert "research_overview" not in out
    assert "Releasing the myeloid brake" in out["interim_overview"]
    assert "What sets the reversal threshold?" in out["interim_overview"]
    assert fake.await_count == 1
    assert fake.await_args is not None
    spec = fake.await_args.kwargs["spec"]
    properties = spec.json_schema["schema"]["properties"]
    assert set(properties) == {"overview", "open_questions"}
    assert set(properties["overview"]["properties"]) == {"research_directions"}
    direction_item = properties["overview"]["properties"]["research_directions"]["items"]
    assert set(direction_item["properties"]) == {"title"}
    assert spec.max_tokens == RESEARCH_OVERVIEW_INTERIM_MAX_TOKENS
    assert spec.max_tokens < RESEARCH_OVERVIEW_MAX_TOKENS


def _context() -> calls.DirectionWaveContext:
    return calls.DirectionWaveContext(
        state=make_state(
            research_goal="Reverse liver fibrosis",
            supervisor_model_name="test/model",
        ),
        hypotheses_summary="1. an idea",
        evidence_corpus_text="evidence-1: a paper",
    )


def _drafted(count: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"Direction {index}",
            "importance": f"Why direction {index} matters.",
            "recent_findings": "",
            "suggested_experiments": [],
            "sub_topics": [],
        }
        for index in range(count)
    ]


def _body() -> dict[str, Any]:
    return {
        "importance": "The developed argument.",
        "recent_findings": "What is already established.",
        "suggested_experiments": ["Knock it down and read out the marker."],
        "sub_topics": [
            {
                "title": "Sub-topic A",
                "why": "Because Y.",
                "what": "Investigate Z.",
                "example_idea": "Knock Z down and read out Y.",
                "specific_questions": ["Does Z cause Y?"],
            }
        ],
    }


async def test_every_drafted_direction_buys_its_own_bounded_call() -> None:
    ask = AsyncMock(return_value={**_body(), "title": "Renamed"})

    developed, spent = await calls.develop_research_directions(
        _context(), _drafted(RESEARCH_OVERVIEW_MAX_DIRECTIONS), ask
    )

    assert spent == RESEARCH_OVERVIEW_MAX_DIRECTIONS
    assert ask.await_count == RESEARCH_OVERVIEW_MAX_DIRECTIONS
    assert all(direction["sub_topics"] for direction in developed)
    assert [d["title"] for d in developed] == [
        f"Direction {index}" for index in range(len(developed))
    ]
    sent = ask.await_args_list[0].kwargs
    assert sent["spec"].max_tokens == RESEARCH_OVERVIEW_DIRECTION_MAX_TOKENS
    assert "options" not in sent
    assert all(
        f"Direction {index}" in sent["prompt"] for index in range(RESEARCH_OVERVIEW_MAX_DIRECTIONS)
    )


async def test_one_failing_call_costs_only_its_own_direction() -> None:
    ask = AsyncMock(side_effect=[RuntimeError("provider exploded"), _body()])

    developed, spent = await calls.develop_research_directions(_context(), _drafted(2), ask)

    assert spent == 2
    assert developed[0]["title"] == "Direction 0"
    assert developed[0]["importance"] == "Why direction 0 matters."
    assert developed[0]["sub_topics"] == []
    assert developed[1]["sub_topics"]
