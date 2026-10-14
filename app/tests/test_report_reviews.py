from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from co_scientist.domains.report.markdown.hypothesis import (
    _render_hypothesis_reviews,
)
from co_scientist.domains.research_state.repository import records

from tests._drain_helpers import (
    _build_report,
    _final_state_with_features,
    _persist_and_finalize,
)
from tests._report_helpers import render_markdown
from tests._store_helpers import seed_run


def _row(agent: str, detail: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "hypothesis_id": "h1",
        "reviewer_agent": agent,
        "detail_json": json.dumps(detail),
        **extra,
    }


def _references() -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "C1",
            {
                "title": "Hexameric resistosome assembly",
                "authors": ["Madhuprakash"],
                "year": 2024,
                "abstract": "Direct evidence for a hexameric helper NLR.",
            },
        )
    ]


def test_all_reviews_prefers_the_latest_row_of_each_agent() -> None:
    # Reviews arrive oldest first; choosing the first would publish a superseded
    # assessment.
    stale = _row("review", {"scores": {"novelty": 1}})
    fresh = _row("review", {"scores": {"novelty": 9}})

    lines = _render_hypothesis_reviews([stale, fresh])

    assert "**Answer: 9**" in lines
    assert "**Answer: 1**" not in lines


_TITLES = {"h1": "SGLT2 inhibition in fibroblasts", "h2": "NHE1 screening"}


def _transcript(verdict: str, turns: list[tuple[int, str, str]]) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "turns": [
                {"turn": turn, "favored": favored, "text": text} for turn, favored, text in turns
            ],
        }
    )


def _match(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "winner_id": "h1",
        "loser_id": "h2",
        "debate_turns": 2,
        "rationale": "Idea 1 prevails.",
        "debate_transcript": _transcript(
            "1",
            [
                (1, "1", "Idea 1 names a measurable target."),
                (2, "1", "The counter-argument does not survive."),
            ],
        ),
    }
    row.update(overrides)
    return row


def _debate_markdown(matches: list[dict[str, Any]] | None) -> str:
    return render_markdown(
        top_hypotheses=[{"id": "h1", "title": _TITLES["h1"]}],
        matches=matches,
        hypothesis_title_by_id=dict(_TITLES),
    )


def test_a_stored_debate_renders_turns_and_one_closing_verdict() -> None:
    markdown = _debate_markdown([_match()])

    assert "## Tournament debates" in markdown
    assert "### Debate 1: 1. SGLT2 inhibition in fibroblasts vs 2. NHE1 screening" in markdown
    assert "**Turn 1 (favors idea 1):** Idea 1 names a measurable target." in (markdown)
    assert "**Turn 2 (favors idea 1):** The counter-argument does not" in (markdown)
    assert "Better idea: 1" in markdown
    assert markdown.count("Better idea:") == 1


@pytest.mark.parametrize(
    "matches",
    [
        None,
        [],
        [_match(debate_transcript=None)],
        # Single-turn matches would crowd multi-turn debates out of the capped
        # section.
        [
            _match(
                debate_turns=1,
                debate_transcript=_transcript("1", [(1, "1", "Idea 1 is stronger.")]),
            )
        ],
        # Transcripts quote both ideas in full; withheld participants would
        # republish gated content.
        [_match(loser_id="h-withheld")],
    ],
    ids=["none", "empty", "no-transcript", "single-turn", "withheld-idea"],
)
def test_matches_that_are_not_debates_render_no_section(
    matches: list[dict[str, Any]] | None,
) -> None:
    assert "Tournament debates" not in _debate_markdown(matches)


def test_drain_persists_detail_json_on_the_review_row(
    isolated_db: str,
) -> None:
    state = _final_state_with_features()
    state["hypotheses"][0]["enrichments"] = {
        "full": {
            "verdict": "sound",
            "go_no_go_recommendation": "Go",
            "time_to_verdict": "Short",
        },
        "simulation": {
            "verdict": "breaks_down",
            "failure_points": ["Substrate saturation."],
            "decisive_step": "Step 4.",
        },
    }
    run = seed_run("detail-json goal")
    _persist_and_finalize(run, state, isolated_db)

    rows = records.list_reviews(run.id, db_path=isolated_db)
    reviews = {r["reviewer_agent"]: r for r in rows}
    full_detail = json.loads(reviews["full_review"]["detail_json"])
    sim_detail = json.loads(reviews["simulation_review"]["detail_json"])
    assert full_detail == {"go_no_go": "Go", "time_to_verdict": "Short"}
    assert sim_detail == {
        "failure_points": ["Substrate saturation."],
        "decisive_step": "Step 4.",
    }
    dv = [
        r
        for r in records.list_reviews(run.id, db_path=isolated_db)
        if r["reviewer_agent"] == "deep_verification"
    ]
    dv_detail = json.loads(dv[0]["detail_json"])
    assert dv_detail["verdict"] == "weakened"
    assert set(dv_detail["probes"][0]) == {
        "question",
        "answer",
        "reasoning",
        "fundamental",
    }

    # Drain/store/render catches reviewer-key mismatches isolated helpers miss.
    # Persistence uses asyncio.run internally, so this check stays synchronous.
    _payload, markdown = asyncio.run(_build_report(run, isolated_db))
    assert "#### Simulation review" in markdown
    assert "**Verdict:** Go" in markdown
    assert "**Time to Verdict:** Short" in markdown
    assert "1. **Failure point:** Substrate saturation." in markdown
