from __future__ import annotations

from typing import Any

import pytest

from co_scientist.api.runs import collections
from co_scientist.domains.research_state.elo import live_leaderboard
from co_scientist.domains.research_state.models import Hypothesis, rank_by_elo
from co_scientist.domains.research_state.models import rank_for_publication as rank_models
from co_scientist.domains.research_state.publication import rank_for_publication
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.platform.db import runs


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "id": "undermined",
            "text": "Z",
            "elo_rating": 1500,
            "win_count": 3,
            "verification_verdict": "undermined",
        },
        {"id": "unplayed", "text": "Y", "elo_rating": 1200},
        {"id": "first-tie", "text": "A", "elo_rating": 1184, "win_count": 1},
        {"id": "second-tie", "text": "Z", "elo_rating": 1184, "loss_count": 1},
        {"id": "top", "text": "B", "elo_rating": 1400, "win_count": 2},
    ]


def test_models_and_leaderboard_share_played_first_stable_order() -> None:
    rows = _rows()
    models = [
        Hypothesis(
            id=row["id"],
            text=row["text"],
            elo_rating=row["elo_rating"],
            win_count=row.get("win_count", 0),
            loss_count=row.get("loss_count", 0),
            deep_verification_verdict=row.get("verification_verdict"),
        )
        for row in rows
    ]
    expected = ["top", "first-tie", "second-tie", "unplayed", "undermined"]
    assert [row["id"] for row in rank_for_publication(rows)] == expected
    assert [h.id for h in rank_models(models)] == expected
    assert [row["id"] for row in live_leaderboard(rows)] == expected
    assert [row["id"] for row in rows] == [
        "undermined",
        "unplayed",
        "first-tie",
        "second-tie",
        "top",
    ]
    assert rank_by_elo(models)[0].id == "undermined"


@pytest.mark.asyncio
async def test_collection_returns_publication_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collections, "_run_or_404", lambda run_id: {})
    monkeypatch.setattr(hypotheses, "list_hypotheses", lambda run_id: _rows())
    monkeypatch.setattr(runs, "run_used_offline", lambda run: True)
    response = await collections.get_hypotheses("run")
    assert [row["id"] for row in response["hypotheses"]] == [
        "top",
        "first-tie",
        "second-tie",
        "unplayed",
        "undermined",
    ]


def test_explicit_zero_rating_has_the_same_meaning_in_models_and_store_rows() -> None:
    models = [
        Hypothesis(text="Zero", id="zero", elo_rating=0, win_count=1),
        Hypothesis(text="Positive", id="positive", elo_rating=10, win_count=1),
    ]
    rows = [{"id": h.id, "elo_rating": h.elo_rating, "win_count": h.win_count} for h in models]
    assert [h.id for h in rank_models(models)] == ["positive", "zero"]
    assert [h["id"] for h in rank_for_publication(rows)] == ["positive", "zero"]
