import asyncio
from itertools import combinations
from typing import Any

import pytest

from co_scientist.science.ranking import ranking_debate
from co_scientist.science.ranking.operations import (
    apply_ranking_matchup,
    judge_ranking_matchup,
    prepare_ranking_judging_context,
    prepare_ranking_prompt_context,
)
from co_scientist.science.ranking.ranking import ranking_node
from tests._state import make_hypothesis, make_ranking_response, make_review, make_state


def _judge(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompts: list[str] = []

    async def answer(*, prompt: str, **_: Any) -> dict[str, Any]:
        prompts.append(prompt)
        # Keep the same idea's vote when its presentation order swaps.
        winner = "a" if prompt.index("mechanism alpha") < prompt.index("mechanism beta") else "b"
        return make_ranking_response(winner, decision_summary="Grounded mechanism wins.")

    monkeypatch.setattr(ranking_debate, "call_llm_json", answer)
    return prompts


async def test_two_idea_tournament_buys_one_call_and_keeps_elo_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [
        make_hypothesis(text=f"mechanism {name}", id=name, reviews=[make_review()])
        for name in ("alpha", "beta")
    ]
    prompts = _judge(monkeypatch)
    result = await ranking_node(make_state(hypotheses=pool, tournament_pairs=1, elo_k_factor=32))

    assert len(prompts) == 1
    matchup = result["tournament_matchups"][0]
    assert matchup["debate_turns"] == 1
    assert sum(h.total_matches for h in pool) == 2
    assert sorted(h.elo_rating for h in pool) == [1184, 1216]
    assert matchup["debate_transcript"][0]["valid_output"] is True


async def test_first_wave_is_one_call_per_pair_even_when_all_seed_elos_tie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def answer(*, prompt: str, **_: Any) -> dict[str, Any]:
        calls.append(prompt)
        return make_ranking_response()

    monkeypatch.setattr(ranking_debate, "call_llm_json", answer)
    pool = [make_hypothesis(text=f"idea {i}", id=str(i)) for i in range(4)]
    context = prepare_ranking_judging_context(prepare_ranking_prompt_context(make_state()), pool)
    pairs = list(combinations(pool, 2))
    results = await asyncio.gather(
        *(judge_ranking_matchup(pair, context, i) for i, pair in enumerate(pairs))
    )

    assert len(calls) == len(pairs) == 6
    assert [result.budgeted_turns for result in results] == [1] * 6


@pytest.mark.parametrize("matchup_index", [0, 1, 7])
async def test_pair_outside_top_k_never_debates(
    monkeypatch: pytest.MonkeyPatch, matchup_index: int
) -> None:
    pool = [
        make_hypothesis(text=f"mechanism {name}", id=name, win_count=1, elo_rating=1500 - i * 50)
        for i, name in enumerate(("alpha", "gamma", "delta", "epsilon", "zeta", "beta"))
    ]
    prompts = _judge(monkeypatch)
    context = prepare_ranking_judging_context(prepare_ranking_prompt_context(make_state()), pool)
    judgement = await judge_ranking_matchup((pool[0], pool[-1]), context, matchup_index)

    assert len(prompts) == 1
    assert judgement.budgeted_turns == 1


async def test_ranked_top_pair_debates_with_swapped_position_consistent_votes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [
        make_hypothesis(text=f"mechanism {name}", id=name, win_count=1)
        for name in ("alpha", "beta")
    ]
    prompts = _judge(monkeypatch)
    context = prepare_ranking_judging_context(prepare_ranking_prompt_context(make_state()), pool)
    judgement = await judge_ranking_matchup((pool[0], pool[1]), context, 1)
    result = apply_ranking_matchup((pool[0], pool[1]), judgement, k_factor=32, current_iteration=0)

    assert len(prompts) == result.llm_calls == 3
    assert judgement.winner == "a"
    transcript = judgement.response["debate_transcript"]
    assert [turn["presentation_order"] for turn in transcript] == ["ba", "ab", "ba"]
    assert {turn["winner_id"] for turn in transcript} == {"alpha"}


async def test_wave_snapshot_does_not_promote_newcomers_after_another_match_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [make_hypothesis(text=f"mechanism {name}") for name in ("alpha", "beta")]
    prompts = _judge(monkeypatch)
    context = prepare_ranking_judging_context(prepare_ranking_prompt_context(make_state()), pool)
    for hypothesis in pool:
        hypothesis.win_count = 1
    judgement = await judge_ranking_matchup((pool[0], pool[1]), context, 1)

    assert len(prompts) == 1
    assert judgement.budgeted_turns == 1


@pytest.mark.parametrize(
    "tier, finalists", [("express", 3), ("standard", 5), ("extended", 6), ("ultra", 8)]
)
def test_tier_finalist_budget_reaches_the_ranking_snapshot(tier: str, finalists: int) -> None:
    from co_scientist.core.run_modes import RUN_TIER_DEFAULTS
    from co_scientist.science.scheduling.models import Budget

    budget = {"finalists": RUN_TIER_DEFAULTS[tier]["finalists"]}
    assert Budget(max_iterations=1, **budget).finalists == finalists
    pool = [make_hypothesis(id=str(i), win_count=1, elo_rating=1400 - i) for i in range(12)]
    prompt = prepare_ranking_prompt_context(make_state(budget=budget))
    context = prepare_ranking_judging_context(prompt, pool)

    assert len(context.debate_ids) == finalists
    assert context.debate_ids == frozenset(str(i) for i in range(finalists))


def test_seed_rating_is_not_a_rank_and_legacy_budget_keeps_five_finalists() -> None:
    pool = [make_hypothesis(id=str(i), win_count=1, elo_rating=1400 - i) for i in range(7)]
    pool.append(make_hypothesis(id="unplayed", elo_rating=2000))
    context = prepare_ranking_judging_context(prepare_ranking_prompt_context(make_state()), pool)

    assert context.debate_ids == frozenset(str(i) for i in range(5))
