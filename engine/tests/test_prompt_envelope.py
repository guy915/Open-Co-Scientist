import json
from typing import Any

import pytest
from jsonschema import validate

from co_scientist.science.generation.literature_review import synthesis
from co_scientist.science.prompts import (
    DebatePromptRequest,
    DraftPromptRequest,
    get_debate_generation_prompt,
    get_draft_prompt_with_tools,
)
from co_scientist.science.prompts.planning import (
    DirectionWritingMaterial,
    get_research_overview_direction_prompt,
)
from co_scientist.science.reflection import comprehensive_reflection as cr
from tests._llm_fake import mock_call_llm_json
from tests._state import make_article, make_hypothesis, make_state


def _evidence() -> str:
    return "\n".join(
        f"- evidence-{i}: title=Experiment {i}; source=pubmed; source_id=pmid-{i}; "
        f"abstract={'The assay observes a dose-dependent effect with rescue controls. ' * 48}"
        for i in range(1, 81)
    )


def _request_bytes(prompt: str, schema: dict[str, object] | None) -> int:
    return len((prompt + json.dumps(schema)).encode())


def _references() -> str:
    return "\n".join(
        f"[C{i}] Author et al., 2023 — " + f"Experiment {i} with rescue controls. " * 60
        for i in range(1, 81)
    )


@pytest.mark.parametrize("paper_budget,omit_last", [(4, False), (4, True), (8, False)])
async def test_fulltext_analysis_bounds_each_request_and_retains_every_paper(
    monkeypatch: pytest.MonkeyPatch, paper_budget: int, omit_last: bool
) -> None:
    papers = {
        f"pmid-{i}": {
            "title": f"Causal rescue study {i}",
            "authors": [f"Researcher {i}"],
            "year": 2024,
            "fulltext": "\n\n".join(
                f"Section {j}: causal rescue assay evidence. " * 80 for j in range(80)
            ),
        }
        for i in range(1, 5)
    }
    packets: list[str] = []

    async def answer(*, prompt: str, **kwargs: Any) -> dict[str, Any]:
        schema = kwargs["spec"].json_schema
        assert _request_bytes(prompt, schema) <= 32_000
        assert "omitted text is not evidence of absence" in prompt
        assert kwargs["spec"].role == "literature_analysis"
        packets.append(prompt)
        analysis = {
            field: f"Retained {field}"
            for field in (
                "key_findings",
                "gaps_identified",
                "future_work",
                "methodology_limitations",
                "unexplored_areas",
                "relevance",
            )
        }
        if "### Paper 1" in prompt:
            return {
                "analyses": [
                    {"paper_index": i, **analysis} for i in range(1, 4 if omit_last else 5)
                ]
            }
        return analysis

    monkeypatch.setattr(synthesis, "call_llm_json", answer)
    analyses = await synthesis._phase3_analyze_papers(
        papers, make_state(literature_review_papers_count=paper_budget)
    )

    expected_calls = (2 if omit_last else 1) if paper_budget == 4 else 4
    assert len(packets) == expected_calls
    assert [entry["paper_id"] for entry in analyses] == list(papers)
    for entry in analyses:
        assert entry["metadata"] is papers[entry["paper_id"]]
        assert len(entry["metadata"]["fulltext"]) > 200_000
        assert set(entry["analysis"]) == {
            "key_findings",
            "gaps_identified",
            "future_work",
            "methodology_limitations",
            "unexplored_areas",
            "relevance",
        }
    if paper_budget == 4:
        for i in range(1, 5):
            assert f"### Paper {i}" in packets[0]
            assert f"Causal rescue study {i}" in packets[0]


async def test_finalist_review_bounds_literature_without_changing_stored_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    literature = _evidence()
    state = make_state(articles_with_reasoning=literature, articles=[])
    fake = mock_call_llm_json(
        monkeypatch,
        cr,
        {
            "observation": {"classification": "neutral"},
            "full_review": {"verdict": "sound"},
            "simulation": {"verdict": "holds"},
            "verification_queries": [],
        },
    )

    run = await cr.review_finalist(state, make_hypothesis())

    assert fake.call_args is not None
    prompt = fake.call_args.kwargs["prompt"]
    schema = fake.call_args.kwargs["spec"].json_schema
    assert _request_bytes(prompt, schema) <= 32_000
    assert "evidence-1" in prompt and "pmid-1" in prompt
    assert "omitted text is not evidence of absence" in prompt
    assert state["articles_with_reasoning"] == literature
    assert run.result is not None
    assert run.result["full_review"]["verdict"] == "sound"
    assert run.result["retrieved_articles"] == []
    assert schema is not None
    assert set(schema["schema"]["required"]) == {
        "full_review",
        "simulation",
        "verification_queries",
    }


def test_direction_writer_bounds_context_and_retains_grounding_handles() -> None:
    prompt, schema = get_research_overview_direction_prompt(
        "Test causal feedback in metabolic disease",
        DirectionWritingMaterial(
            "Selective rescue", "Discriminate causal feedback", "Selective rescue"
        ),
        "\n".join(
            f"Idea {i}: " + "A supported mechanism with a falsifiable control. " * 50
            for i in range(20)
        ),
        _evidence(),
    )

    assert _request_bytes(prompt, schema) <= 32_000
    assert "evidence-1" in prompt
    assert "pmid-1" in prompt
    assert "Selective rescue" in prompt
    for i in range(20):
        assert f"Idea {i}:" in prompt
    assert schema is not None
    assert "suggested_experiments - a JSON array of 2-4 strings" in prompt


@pytest.mark.parametrize("final_turn", [False, True])
def test_generation_debate_sends_bounded_turn_summaries(final_turn: bool) -> None:
    transcript = "".join(
        f"\n\nTurn {i}:\n"
        + f"Objection {i}: causal uncertainty. " * 400
        + f"\nResolution {i}: retain the rescue control."
        for i in range(1, 11)
    )
    prompt, schema = get_debate_generation_prompt(
        DebatePromptRequest(
            research_goal="Test causal feedback in metabolic disease",
            transcript=transcript,
            articles_with_reasoning=_evidence(),
            reference_list=_references(),
            is_final_turn=final_turn,
        )
    )

    assert _request_bytes(prompt, schema) <= 32_000
    assert "Turn 1" in prompt
    assert "Turn 10" in prompt
    assert "Resolution 10" in prompt
    for i in range(1, 81):
        assert f"[C{i}]" in prompt
    if final_turn:
        assert schema is not None
        assert "literature_grounding" in prompt
        assert "falsification" in prompt.lower()
        example_text = prompt.split("Output exactly 1 hypothesis as valid JSON:\n", 1)[1]
        example, _ = json.JSONDecoder().raw_decode(example_text)
        validate(example, schema["schema"])


def test_generation_draft_bounds_repeated_literature_context() -> None:
    prompt, schema = get_draft_prompt_with_tools(
        DraftPromptRequest(
            research_goal="Test causal feedback in metabolic disease",
            hypotheses_count=3,
            articles_with_reasoning=_evidence(),
            articles=[
                make_article(title=f"Experiment {i}", used_in_analysis=True) for i in range(80)
            ],
            reference_list=_references(),
            lab_constraints=["Use primary cells; no animal experiments."],
        )
    )

    assert _request_bytes(prompt, schema) <= 32_000
    for i in range(1, 81):
        assert f"[C{i}]" in prompt
    assert "no animal experiments" in prompt
    assert schema is not None


def test_meta_review_summarizes_history_without_losing_votes_or_assessments() -> None:
    from co_scientist.science.prompts.context_budget import summarize_feedback

    records: list[dict[str, Any]] = [
        {
            "record_type": "review_history",
            "hypothesis_index": i,
            "hypothesis_text": f"Mechanism {i}: " + "causal rescue evidence " * 200,
            "elo_rating": 1200 + i,
            "deep_verification_verdict": "unsupported" if i == 2 else "supported",
            "reviews": [
                {"score": 2, "reasoning": "Old uncertain result " * 300},
                {"score": 4, "reasoning": "Latest rescue result " * 300},
            ],
        }
        for i in range(1, 21)
    ]
    records.append(
        {
            "record_type": "ranking_debate",
            "hypothesis_a_id": "idea-1",
            "hypothesis_b_id": "idea-2",
            "winner_id": "idea-1",
            "confidence": 0.8,
            "reasoning": "Rescue distinguishes causality " * 300,
            "debate_transcript": [
                {
                    "turn": i,
                    "winner_id": "idea-1",
                    "presentation_order": order,
                    "valid_output": True,
                    "decision_summary": "Rescue supports causality " * 300,
                }
                for i, order in enumerate(("ab", "ba", "ab"), 1)
            ],
        }
    )
    serialized = json.dumps(records)
    summarized = summarize_feedback(serialized)
    tables = json.loads(summarized)
    parsed = [
        dict(zip(table["columns"], row, strict=True))
        for table in tables.values()
        if "rows" in table
        for row in table["rows"]
    ]

    assert len(summarized) <= 10_000
    assert [record["hypothesis_index"] for record in parsed[:-1]] == list(range(1, 21))
    assert parsed[1]["deep_verification_verdict"] == "unsupported"
    assert parsed[0]["latest_score"] == 4
    assert parsed[0]["review_count"] == 2
    assert parsed[-1]["debate_transcript"]["presentation_order"] == [
        "ab",
        "ba",
        "ab",
    ]
    assert parsed[-1]["winner_id"] == "idea-1"
    assert records[0]["reviews"][0]["score"] == 2


def test_overview_summary_keeps_every_numbered_idea_and_elo() -> None:
    from co_scientist.science.prompts.context_budget import summarize_hypotheses

    summary = "\n".join(
        f"{i}. (Elo {1200 + i}) " + f"Mechanism {i} with controls. " * 200 for i in range(1, 11)
    )
    result = summarize_hypotheses(summary)

    assert len(result) <= 6_000
    for i in range(1, 11):
        assert f"{i}. (Elo {1200 + i})" in result
    assert "omitted text is not evidence of absence" in result


def test_evolution_bounds_evidence_but_keeps_every_reference_key() -> None:
    from co_scientist.science.citations import ReferenceIndex
    from co_scientist.science.evolution import EvolutionContext
    from co_scientist.science.evolution.evolve_prompt import (
        _build_evolution_prompt,
        _EvolutionOperation,
    )
    from tests._state import make_hypothesis, make_state

    references = "\n".join(
        f"[C{i}] Author et al., 2023 - " + f"Mechanism {i} with rescue controls. " * 60
        for i in range(1, 129)
    )
    index = ReferenceIndex(text=references, sources={})
    prompt, schema = _build_evolution_prompt(
        make_hypothesis(text="Retain the causal rescue control."),
        [],
        EvolutionContext(
            model_name="test-model",
            meta_review={},
            removed_duplicates=[],
            state=make_state(),
            articles_with_reasoning=_evidence(),
            reference_index=index,
        ),
        _EvolutionOperation(),
        grounding_evidence=_evidence(),
    )

    assert _request_bytes(prompt, schema) <= 32_000
    for i in range(1, 129):
        assert f"[C{i}]" in prompt
    assert "Retain the causal rescue control." in prompt
    assert "evidence-1" in prompt
    assert index.text == references
    assert schema is not None


def test_compact_context_preserves_bare_ids_when_required_fields_exceed_the_cap() -> None:
    from co_scientist.science.prompts.context_budget import compact_json_context

    record = {
        "id": "00000000-0000-0000-0000-000000000001",
        "claim_id": "00000000-0000-0000-0000-000000000002",
        "verdict": "sound",
        "classification": "other explanations more likely",
        "reasoning": "A long evidence explanation. " * 200,
    }
    compact = json.loads(compact_json_context(json.dumps(record), 120))

    assert set(compact) == set(record)
    assert compact["id"] == record["id"]
    assert compact["claim_id"] == record["claim_id"]
    assert compact["verdict"] == "sound"
    assert compact["classification"] == record["classification"]
    assert "[excerpt]" in compact["reasoning"]


def test_compact_context_reaches_its_prose_floor_without_losing_records() -> None:
    from co_scientist.science.prompts.context_budget import compact_json_context

    records = [
        {"score": i, "reasoning": "Evidence from rescue controls. " * 100} for i in range(100)
    ]

    serialized = compact_json_context(json.dumps(records), 4_800)
    compact = json.loads(serialized)

    assert len(serialized) <= 4_800
    assert [record["score"] for record in compact] == list(range(100))
    assert all(set(record) == {"score", "reasoning"} for record in compact)
    assert all("[excerpt]" in record["reasoning"] for record in compact)


@pytest.mark.parametrize("limit", [600, 30_000])
def test_feedback_deduplicates_identifiers_without_losing_records(limit: int) -> None:
    from co_scientist.science.prompts.context_budget import summarize_feedback

    first = "12345678-1234-4234-8234-123456789abc"
    second = "87654321-4321-4321-8321-cba987654321"
    records = [
        {
            "record_type": "ranking_debate",
            "match_index": index,
            "hypothesis_a_id": first,
            "hypothesis_b_id": second,
            "winner_id": first if index % 2 else second,
            "confidence": 0.75,
            "reasoning": "Supported",
            "opaque_label": "record_1_id",
        }
        for index in range(1, 13)
    ]
    rendered = summarize_feedback(json.dumps(records), limit=limit)
    assert rendered.count(first) == rendered.count(second) == 1
    summary = json.loads(rendered)
    identifiers = summary.pop("record_ids")

    def restore(value: object) -> object:
        if isinstance(value, str):
            return identifiers.get(value, value)
        if isinstance(value, list):
            return [restore(item) for item in value]
        if isinstance(value, dict):
            return {identifiers.get(key, key): restore(item) for key, item in value.items()}
        return value

    restored = restore(summary)
    assert isinstance(restored, dict)
    history = restored["ranking_history_summary"]
    assert history["match_count"] == 12
    assert history["winner_counts"] == {first: 6, second: 6}
    table = restored["ranking_debate"]
    expected = [
        {key: value for key, value in record.items() if key != "record_type"}
        for record in records[-8:]
    ]
    assert [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]] == expected
