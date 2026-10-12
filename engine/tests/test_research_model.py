from __future__ import annotations

import json

import pytest

from co_scientist.platform.llm.offline import llm as offline_llm
from co_scientist.science.prompts import load_prompt_with_schema
from co_scientist.science.research_model import LlmResearchModel


class RecordingModel(LlmResearchModel):
    def __init__(self, perspectives: object) -> None:
        super().__init__("offline")
        self.perspectives = perspectives
        self.calls: list[str] = []

    async def _ask(
        self, prompt_name: str, variables: dict[str, object], max_tokens: int
    ) -> dict[str, object]:
        self.calls.append(prompt_name)
        replies: dict[str, dict[str, object]] = {
            "research_plan": {"perspectives": self.perspectives},
            "research_stances": {"stances": ["mechanism", "counter-evidence"]},
            "research_questions": {"questions": ["What inhibits protein X folding?"]},
            "research_query": {"query": "protein X folding inhibition"},
        }
        return replies[prompt_name]


_PLAN = [
    {"stance": "mechanism", "question": "How does protein X fold?", "query": "protein X folding"},
    {
        "stance": "counter-evidence",
        "question": "What inhibits protein X folding?",
        "query": "protein X folding inhibition",
    },
]


@pytest.mark.asyncio
async def test_research_plan_retains_every_branch_in_one_request() -> None:
    model = RecordingModel(_PLAN)
    stances = await model.plan_stances(goal="Protein X", limit=2)
    assert list(stances) == ["mechanism", "counter-evidence"]
    for stance, expected in zip(stances, _PLAN, strict=True):
        questions = await model.ask_questions(goal="Protein X", stance=stance, limit=1)
        assert list(questions) == [expected["question"]]
        assert await model.to_query(question=questions[0]) == expected["query"]
    assert model.calls == ["research_plan"]


@pytest.mark.asyncio
async def test_partial_plan_preserves_stances_and_falls_back_only_for_missing_parts() -> None:
    model = RecordingModel([{"stance": "mechanism"}, _PLAN[1]])
    stances = await model.plan_stances(goal="Protein X", limit=2)
    assert len(stances) == 2
    questions = await model.ask_questions(goal="Protein X", stance=stances[0], limit=1)
    await model.to_query(question=questions[0])
    assert model.calls == ["research_plan", "research_questions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [None, [], [{"question": "No stance"}], "invalid"])
async def test_unusable_plan_retains_legacy_stance_planning(malformed: object) -> None:
    model = RecordingModel(malformed)
    assert list(await model.plan_stances(goal="Protein X", limit=2)) == [
        "mechanism",
        "counter-evidence",
    ]
    assert model.calls == ["research_plan", "research_stances"]


@pytest.mark.asyncio
async def test_uncached_questions_and_larger_question_requests_use_existing_calls() -> None:
    model = RecordingModel(_PLAN)
    await model.plan_stances(goal="Protein X", limit=2)
    await model.ask_questions(goal="Protein X", stance="mechanism", limit=2)
    await model.ask_questions(goal="Another goal", stance="mechanism", limit=1)
    await model.to_query(question="A follow-up question")
    assert model.calls == [
        "research_plan",
        "research_questions",
        "research_questions",
        "research_query",
    ]


@pytest.mark.asyncio
async def test_replanning_clears_queries_and_conflicting_queries_do_not_use_last_writer() -> None:
    model = RecordingModel(_PLAN)
    await model.plan_stances(goal="Protein X", limit=2)
    model.perspectives = [
        {"stance": "one", "question": "Same question", "query": "first query"},
        {"stance": "two", "question": "Same question", "query": "second query"},
    ]
    await model.plan_stances(goal="Another goal", limit=2)
    await model.to_query(question="How does protein X fold?")
    await model.to_query(question="Same question")
    assert model.calls == ["research_plan", "research_plan", "research_query", "research_query"]


@pytest.mark.asyncio
async def test_research_planning_does_not_swallow_budget_exhaustion() -> None:
    class Exhausted(RecordingModel):
        async def _ask(
            self, prompt_name: str, variables: dict[str, object], max_tokens: int
        ) -> dict[str, object]:
            raise RuntimeError("budget exhausted")

    with pytest.raises(RuntimeError, match="budget exhausted"):
        await Exhausted(_PLAN).plan_stances(goal="Protein X", limit=2)


@pytest.mark.asyncio
async def test_valid_question_with_no_query_uses_the_focused_query_call() -> None:
    model = RecordingModel([{"stance": "mechanism", "question": "How does protein X fold?"}])
    await model.plan_stances(goal="Protein X", limit=1)
    questions = await model.ask_questions(goal="Protein X", stance="mechanism", limit=1)
    await model.to_query(question=questions[0])
    assert model.calls == ["research_plan", "research_query"]


@pytest.mark.asyncio
async def test_duplicate_and_excess_stances_do_not_expand_the_budget() -> None:
    model = RecordingModel([_PLAN[0], {**_PLAN[0], "stance": "MECHANISM"}, _PLAN[1]])
    assert list(await model.plan_stances(goal="Protein X", limit=1)) == ["mechanism"]
    assert model.calls == ["research_plan"]


@pytest.mark.asyncio
async def test_conflicting_questions_for_one_stance_use_the_focused_question_call() -> None:
    model = RecordingModel([_PLAN[0], {**_PLAN[0], "question": "A different question"}])
    await model.plan_stances(goal="Protein X", limit=1)
    await model.ask_questions(goal="Protein X", stance="mechanism", limit=1)
    assert model.calls == ["research_plan", "research_questions"]


def test_offline_planning_exercises_complete_branches_without_reducing_stance_count() -> None:
    counts = []
    for name, field in (("research_stances", "stances"), ("research_plan", "perspectives")):
        prompt, schema = load_prompt_with_schema(name, {"research_goal": "Protein X", "limit": 2})
        assert schema is not None
        response = offline_llm._schema_response("offline", prompt, schema)
        data = json.loads(response.choices[0].message.content)
        counts.append(len(data[field]))
        if name == "research_plan":
            assert all(
                all(record.get(key) for key in ("stance", "question", "query"))
                for record in data[field]
            )
    assert counts == [1, 1]
