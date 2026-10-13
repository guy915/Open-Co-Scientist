from __future__ import annotations

from typing import Any

import pytest

from co_scientist.science.generation.literature_review import synthesis
from tests._state import make_state

_ANALYSIS = {
    "key_findings": "finding",
    "gaps_identified": "gap",
    "future_work": "next",
    "methodology_limitations": "limit",
    "unexplored_areas": "open",
    "relevance": "relevant",
}


def _papers(count: int) -> dict[str, dict[str, Any]]:
    return {f"p{i}": {"title": f"Paper {i}", "abstract": f"Abstract {i}."} for i in range(count)}


def _answering(batch: dict[str, Any] | Exception) -> tuple[list[str], Any]:
    prompts: list[str] = []

    async def answer(*, prompt: str, **kwargs: Any) -> dict[str, Any]:
        prompts.append(kwargs["options"].prompt_name if kwargs.get("options") else "single")
        if "### Paper 1" in prompt:
            if isinstance(batch, Exception):
                raise batch
            return batch
        return dict(_ANALYSIS, key_findings="single-paper finding")

    return prompts, answer


async def test_express_analyzes_its_papers_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = {"analyses": [{"paper_index": n, **_ANALYSIS} for n in (1, 2, 3)]}
    prompts, answer = _answering(batch)
    monkeypatch.setattr(synthesis, "call_llm_json", answer)

    analyses = await synthesis._phase3_analyze_papers(
        _papers(3), make_state(literature_review_papers_count=4)
    )

    assert prompts == ["literature_review_paper_analysis_batch"]
    assert [a["paper_id"] for a in analyses] == ["p0", "p1", "p2"]
    assert analyses[0]["analysis"] == _ANALYSIS


async def test_a_paper_the_batch_omits_is_analyzed_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = {"analyses": [{"paper_index": n, **_ANALYSIS} for n in (1, 3, 9)]}
    prompts, answer = _answering(batch)
    monkeypatch.setattr(synthesis, "call_llm_json", answer)

    analyses = await synthesis._phase3_analyze_papers(
        _papers(3), make_state(literature_review_papers_count=4)
    )

    assert len(prompts) == 2
    assert [a["paper_id"] for a in analyses] == ["p0", "p1", "p2"]
    assert analyses[1]["analysis"]["key_findings"] == "single-paper finding"


async def test_a_failed_batch_falls_back_to_one_call_per_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts, answer = _answering(RuntimeError("bad answer"))
    monkeypatch.setattr(synthesis, "call_llm_json", answer)

    analyses = await synthesis._phase3_analyze_papers(
        _papers(3), make_state(literature_review_papers_count=4)
    )

    assert len(prompts) == 4
    assert len(analyses) == 3


async def test_larger_tiers_keep_one_call_per_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts, answer = _answering(RuntimeError("never called"))
    monkeypatch.setattr(synthesis, "call_llm_json", answer)

    await synthesis._phase3_analyze_papers(_papers(3), make_state(literature_review_papers_count=8))

    assert prompts == ["single"] * 3
