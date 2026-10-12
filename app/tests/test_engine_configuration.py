from __future__ import annotations

from typing import Any, ClassVar

import pytest
from co_scientist.core.config import (
    PROVIDER_CREDENTIAL_ENV,
)
from co_scientist.core.run_modes import (
    resolved_run_config,
)
from co_scientist.domains.chat.repository import messages as store
from co_scientist.domains.research_state.elo import (
    rank_for_publication as app_rank_for_publication,
)
from co_scientist.domains.research_state.models import UNDERMINED_VERDICT, Hypothesis
from co_scientist.domains.research_state.models import (
    rank_for_publication as engine_rank_for_publication,
)
from co_scientist.orchestration.engine_adapter.events import (
    _canonical_engine_payload,
    _canonical_event_type,
    append_node_milestone,
)
from co_scientist.orchestration.engine_adapter.opts import (
    build_engine_opts,
    build_generator,
)
from co_scientist.orchestration.generator.core import HypothesisGenerator
from co_scientist.platform.llm import process_mode

from tests._store_helpers import seed_run


@pytest.mark.parametrize(
    ("tier", "resolved", "funded"),
    [
        ("express", "express", False),
        ("standard", "standard", False),
        ("extended", "extended", True),
        ("ultra", "ultra", True),
        (None, "standard", False),
    ],
)
def test_tier_funding_reaches_every_expensive_capability(
    isolated_db: str, tier: str | None, resolved: str, funded: bool
) -> None:
    opts = build_engine_opts({"tier": tier}, "unused-run", isolated_db)
    assert opts["research_tier"] == resolved
    assert opts["enable_tool_calling_generation"] is funded
    assert opts["enable_simulation_execution"] is funded
    assert opts["enable_overview_review"] is funded


@pytest.mark.parametrize("enabled", [False, True])
def test_literature_kill_switch_overrides_the_connector(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    cfg = {"enable_literature_review": enabled}
    monkeypatch.delenv("FORCE_LITERATURE_REVIEW", raising=False)
    opts = build_engine_opts(cfg, "unused-run", isolated_db)
    assert opts["enable_literature_review_node"] is enabled
    monkeypatch.setenv("FORCE_LITERATURE_REVIEW", "0")
    opts = build_engine_opts(cfg, "unused-run", isolated_db)
    assert opts["enable_literature_review_node"] is False


_ENGINE_NODES = [
    "supervisor",
    "literature_review",
    "generate",
    "reflection",
    "review",
    "ranking",
    "deep_verification",
    "meta_review",
    "evolve",
    "proximity",
    "research_overview",
]
_EXPECTED_TYPES = {
    "supervisor": "supervisor.plan",
    "literature_review": "literature_review",
    "generate": "generate",
    "reflection": "reflection",
    "review": "review",
    "ranking": "ranking",
    "deep_verification": "deep_verification",
    "meta_review": "meta_review",
    "evolve": "evolve",
    "proximity": "proximity",
    "research_overview": "research_overview",
}


def _streaming_hypotheses() -> list[dict[str, Any]]:
    return [
        {
            "id": "eng-h1",
            "text": "H1: a mechanistic claim about the pathway.",
            "elo_rating": 1300,
            "win_count": 2,
            "loss_count": 0,
            "evolution_history": [],
            "reviews": [{"review_summary": "Sound mechanism."}],
            "deep_verification_verdict": "verified",
            "deep_verification_probes": [{"question": "Does X cause Y?", "answer": "Yes, via Z."}],
        },
        {
            "id": "eng-h2",
            "text": "H2: an evolved variant of the leading claim.",
            "elo_rating": 1250,
            "win_count": 1,
            "loss_count": 1,
            "evolution_history": [{"round": 1}],
            "reviews": [],
        },
    ]


def _streaming_research_overview() -> dict[str, Any]:
    return {
        "overview": {"summary": "Targeting the pathway looks promising."},
        "nih_specific_aims": {
            "disease_description": "Background.",
            "aims": [],
        },
    }


def _streaming_proximity_graph() -> dict[str, Any]:
    return {
        "edges": [
            {
                "source": "eng-h1",
                "target": "eng-h2",
                "similarity": 0.8,
                "cluster_id": "cluster-0",
            }
        ],
        "meta": {"method": "embedding", "version": 1},
    }


def _engine_streaming_state() -> dict[str, Any]:
    return {
        "hypotheses": _streaming_hypotheses(),
        "articles": [
            {
                "title": "A1",
                "url": "https://example.org/a1",
                "abstract": (
                    "H1: a mechanistic claim about the pathway. H2: an "
                    "evolved variant of the leading claim."
                ),
            }
        ],
        "tournament_matchups": [
            {
                "hypothesis_a": "H1: a mechanistic claim about the pathway.",
                "hypothesis_b": "H2: an evolved variant of the leading claim.",
                "hypothesis_a_id": "eng-h1",
                "hypothesis_b_id": "eng-h2",
                "winner_id": "eng-h1",
                "winner": "a",
            }
        ],
        "meta_review": {
            "summary": "Leading hypotheses converge on one mechanism.",
            "common_strengths": ["Clear mechanism"],
            "common_weaknesses": ["Thin evidence"],
        },
        "evolution_details": [],
        "research_overview": _streaming_research_overview(),
        "proximity_graph": _streaming_proximity_graph(),
        "current_iteration": 1,
    }


def _payloads_by_type() -> dict[str, dict[str, Any]]:
    state = _engine_streaming_state()
    by_type: dict[str, dict[str, Any]] = {}
    for node in _ENGINE_NODES:
        node_type = _canonical_event_type(node)
        by_type[node_type] = _canonical_engine_payload(node, node_type, state)
    return by_type


def _assert_canonical_event_types(types_emitted: list[str]) -> None:
    assert not any(t.startswith("engine.") for t in types_emitted)

    for node, expected in _EXPECTED_TYPES.items():
        assert expected in types_emitted, f"{node} -> {expected} missing"


def _assert_normalized_payloads(by_type: dict[str, Any]) -> None:
    generate = by_type["generate"]
    assert generate["count"] == 2
    assert len(generate["hypotheses"]) == 2
    assert isinstance(by_type["ranking"]["matches"], list)
    assert len(by_type["ranking"]["matches"]) == 1
    assert len(by_type["evolve"]["children"]) == 1
    assert by_type["literature_review"]["count"] == 1
    assert len(by_type["literature_review"]["evidence"]) == 1
    assert by_type["supervisor.plan"]["agents"]


def _assert_full_fidelity_payloads(by_type: dict[str, Any]) -> None:
    assert by_type["reflection"]["reviewed"] == 1
    assert by_type["proximity"]["clusters"] == {"cluster-0": 2}
    assert by_type["meta_review"]["critique"] == "Leading hypotheses converge on one mechanism."
    assert by_type["meta_review"]["top_k_ids"] == ["eng-h1", "eng-h2"]
    assert by_type["deep_verification"]["verified"] == 1
    assert by_type["deep_verification"]["probes"] == [
        {
            "hypothesis_id": "eng-h1",
            "verdict": "verified",
            "probes": [{"question": "Does X cause Y?", "answer": "Yes, via Z."}],
        }
    ]
    assert by_type["research_overview"]["research_overview"] == _streaming_research_overview()


def test_engine_adapter_emits_canonical_event_types(isolated_db: str) -> None:
    # Offline CI does not drive every live node; direct mapping coverage
    # protects frontend event vocabulary.
    by_type = _payloads_by_type()

    _assert_canonical_event_types(list(by_type))
    _assert_normalized_payloads(by_type)
    _assert_full_fidelity_payloads(by_type)

    run = seed_run("Milestone goal")
    for node_type, payload in by_type.items():
        append_node_milestone(run.id, node_type, payload, db_path=isolated_db)
    msgs = store.list_messages(run.id, db_path=isolated_db)
    text = " | ".join(m.content for m in msgs if m.kind == "milestone")
    assert "Research plan ready" in text
    assert "2 hypotheses generated" in text
    assert "1 matches" in text
    assert "1 hypotheses reviewed" in text
    assert "1 clusters identified" in text
    assert "1 hypotheses verified" in text
    assert "Research overview ready" in text


@pytest.mark.parametrize(
    ("degradation", "carried"),
    [
        (
            {
                "reason": "mcp_unreachable",
                "lost": ["literature_review"],
                "floor": "none",
            },
            True,
        ),
        (None, False),
    ],
)
def test_a_retrieval_outage_rides_every_event_after_it(
    degradation: dict[str, Any] | None, carried: bool
) -> None:
    # Skipped literature stages emit nothing; carry capability loss on later
    # events so absence is not mistaken for delay.
    state: dict[str, Any] = {"current_iteration": 1}
    if degradation:
        state["retrieval_degradation"] = degradation

    payload = _canonical_engine_payload("generate", "generate", state)

    assert ("retrieval_degraded" in payload) is carried


# Each run needs fresh tool topology; process singletons otherwise leak
# connector choices across runs.


def _generator_for(**overrides: Any) -> Any:
    return build_generator(
        HypothesisGenerator,
        resolved_run_config(dict(overrides)),
    )


def _enabled_tools(generator: Any) -> set[str]:
    return set(generator._tool_registry.get_enabled_tools())


def _search_sources(generator: Any) -> set[str]:
    # Source enabled flags control retrieval independently of registered tools.
    workflow = generator._tool_registry.get_workflow("literature_review")
    return {source.tool for source in workflow.get_enabled_search_sources()}


def test_each_run_gets_its_own_connector_topology() -> None:
    # Process singletons otherwise leak connector choices across runs.
    seen = []
    for enabled in (False, True, True, False):
        generator = _generator_for(enable_web_search=enabled)
        seen.append(generator)
        assert ("web_search" in _enabled_tools(generator)) is enabled
        assert ("web_search" in _search_sources(generator)) is enabled

    registries = {id(generator._tool_registry) for generator in seen}
    assert len(registries) == len(seen)


class _FakeGenerator:
    last_kwargs: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        _FakeGenerator.last_kwargs = kwargs


_ALL_CREDENTIAL_ENV = tuple(name for names in PROVIDER_CREDENTIAL_ENV.values() for name in names)


def _clear_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ALL_CREDENTIAL_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    ("force_offline", "has_key", "expected"),
    [
        (None, True, False),
        (None, False, False),
        ("deterministic", True, True),
    ],
    ids=[
        "real_when_key_present",
        "real_without_provider_key",
        "explicit_test_double",
    ],
)
def test_offline_mode(
    monkeypatch: pytest.MonkeyPatch,
    force_offline: str | None,
    has_key: bool,
    expected: bool,
) -> None:
    monkeypatch.delenv("COSCIENTIST_TEST_DOUBLE", raising=False)
    if force_offline is not None:
        monkeypatch.setenv("COSCIENTIST_TEST_DOUBLE", force_offline)
    _clear_credentials(monkeypatch)
    if has_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert process_mode.offline_mode() is expected


# Raw Elo and publication order disagree here: the leader is undermined and
# the runner-up has never played a match.
_MIXED_POOL: list[dict[str, Any]] = [
    {"id": "undermined-top", "elo_rating": 1400, "win_count": 5, "loss_count": 0},
    {"id": "unplayed-high", "elo_rating": 1200, "win_count": 0, "loss_count": 0},
    {"id": "played-high", "elo_rating": 1230, "win_count": 4, "loss_count": 1},
    {"id": "played-low", "elo_rating": 1184, "win_count": 3, "loss_count": 2},
]


def test_meta_review_top_k_follows_the_publication_order() -> None:
    """The narrative's leading ideas and the report leaderboard read the same
    run; raw Elo would lead with an undermined or untested idea.
    """
    pool = [
        {**row, "verification_verdict": "undermined" if row["id"] == "undermined-top" else None}
        for row in _MIXED_POOL
    ]
    state = {"meta_review": {"summary": "converging"}, "hypotheses": pool}

    payload = _canonical_engine_payload("meta_review", "meta_review", state)

    assert payload["top_k_ids"] == [
        "played-high",
        "played-low",
        "unplayed-high",
        "undermined-top",
    ]


def test_the_engine_and_the_app_publish_the_same_order() -> None:
    """Two implementations of one rule: the engine's research overview and
    the app's report must not order the same run differently. The drain maps
    deep_verification_verdict onto the store's verification_verdict, so each
    side is given its own field names for the same pool.
    """
    engine_pool = [
        Hypothesis(
            text=f"idea {row['id']}",
            id=row["id"],
            elo_rating=row["elo_rating"],
            win_count=row["win_count"],
            loss_count=row["loss_count"],
            deep_verification_verdict=(
                UNDERMINED_VERDICT if row["id"] == "undermined-top" else None
            ),
        )
        for row in _MIXED_POOL
    ]
    app_pool = [
        {**row, "verification_verdict": "undermined" if row["id"] == "undermined-top" else None}
        for row in _MIXED_POOL
    ]

    engine_order = [h.id for h in engine_rank_for_publication(engine_pool)]
    app_order = [str(h["id"]) for h in app_rank_for_publication(app_pool)]

    assert engine_order == app_order
