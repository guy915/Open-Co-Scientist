from __future__ import annotations

from typing import Any

from co_scientist.core.config import settings
from co_scientist.core.constants import INITIAL_ELO_RATING
from co_scientist.domains.research_state.publication import (
    rank_for_publication as rank_for_publication,
)
from co_scientist.domains.research_state.text_utils import coalesce, hypothesis_id, hypothesis_title

# Initial Elo stays engine-owned; the app owns deployment-tunable K-factor.
INITIAL_ELO: int = INITIAL_ELO_RATING
DEFAULT_K_FACTOR: int = settings.elo_k_factor


def _leaderboard_row(rank: int, h: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": rank,
        "id": hypothesis_id(h),
        "title": hypothesis_title(h),
        "elo": int(coalesce(h.get("elo_rating", INITIAL_ELO), default=INITIAL_ELO)),
        "wins": int(coalesce(h.get("win_count", 0), default=0)),
        "losses": int(coalesce(h.get("loss_count", 0), default=0)),
    }


def live_leaderboard(hyps: list[dict[str, Any]], cap: int = 10) -> list[dict[str, Any]]:
    ordered = rank_for_publication(hyps)
    return [_leaderboard_row(rank, h) for rank, h in enumerate(ordered[:cap], start=1)]
