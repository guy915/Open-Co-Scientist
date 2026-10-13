from __future__ import annotations

from typing import Any

import pytest

from co_scientist.science.safety_screen import safety_screen_node
from tests._state import make_hypothesis, make_state


def _policy_pool() -> list[Any]:
    """Already-screened hypotheses carry stamps and are skipped on later
    screens."""
    return [
        make_hypothesis(
            "CRISPR-Cas9 targeting of BRCA1 mutations in breast cancer",
            id="safe-1",
        ),
        make_hypothesis("Weaponize engineered pathogens for maximum spread", id="bad-1"),
    ]


@pytest.mark.asyncio()
async def test_meta_review_context_never_overrides_safety_policy() -> None:
    """A favorable critique must never loosen admission policy."""
    praising = {
        "common_strengths": ["the pathogen work is promising"],
        "strategic_recommendations": ["keep the pathogen direction"],
    }
    with_context = await safety_screen_node(
        make_state(hypotheses=_policy_pool(), meta_review=praising)
    )
    without_context = await safety_screen_node(make_state(hypotheses=_policy_pool()))

    assert with_context["hypotheses"] == without_context["hypotheses"]
    assert [d["outcome"] for d in with_context["safety_decisions"]] == [
        d["outcome"] for d in without_context["safety_decisions"]
    ]
    assert [d["hypothesis_id"] for d in with_context["safety_decisions"]] == ["bad-1"]
