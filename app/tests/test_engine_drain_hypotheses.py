from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import co_scientist.platform.retrieval.citations as citation_resolver
import pytest
from co_scientist.core.config import settings
from co_scientist.domains.report import markdown as report_markdown
from co_scientist.domains.research_state.claims.grounding import evidence_passages
from co_scientist.domains.research_state.drain.matches import _persist_engine_matches
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.research_state.repository import hypotheses, records
from co_scientist.domains.safety import gate as safety
from co_scientist.domains.safety.hypothesis import screen_hypotheses
from co_scientist.platform import db
from co_scientist.platform.db.models import RunRow
from co_scientist.platform.retrieval.citations import (
    CitationMetadata,
    Resolvability,
    Resolver,
)
from co_scientist.science.ranking.ranking_debate import (
    _DebateRun,
    _finalize_debate_response,
    _MatchupPrompt,
    build_matchup,
)

from tests._drain_helpers import (
    _engine_hypothesis,
    _final_state_with_features,
    _held_final_state,
    _persist,
    _persist_and_finalize,
)
from tests._llm_fake_backend import semantic_response as _fake_semantic_response
from tests._process_mode_helpers import FakeProcessMode
from tests._store_helpers import seed_run

from ._llm_fake_backend import install_completion_backend

_HELD_TEXT = (
    "Improving hospital triage protocols and resource allocation for "
    "mass casualty events such as natural disasters."
)


def _escalation_state(held_text: str = _HELD_TEXT) -> dict[str, Any]:
    return {
        "hypotheses": [
            _engine_hypothesis(
                "safe-1",
                "Inhibiting kinase X reduces AML growth via apoptosis.",
            ),
            _engine_hypothesis("held-1", held_text),
        ],
        "articles": [],
        "tournament_matchups": [],
        "meta_review": {},
        "evolution_details": [],
        "research_overview": {},
    }


def _real_run(goal: str) -> RunRow:
    return seed_run(goal)


def _stub_eligible(monkeypatch: pytest.MonkeyPatch, fake_process_mode: FakeProcessMode) -> None:
    monkeypatch.setattr(safety, "_should_escalate_to_semantic", lambda *a, **k: True)
    fake_process_mode.online()


def _held_status(run_id: str, isolated_db: str) -> str:
    by_id = {h["id"]: h for h in hypotheses.list_hypotheses(run_id, db_path=isolated_db)}
    return str(by_id["held-1"]["safety_status"])


@pytest.mark.parametrize(
    ("verdict", "status", "decision"),
    [("prohibited", "prohibited", "block"), ("allowed", None, "allow")],
)
def test_drain_escalates_and_audits_the_resolved_verdict(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_process_mode: FakeProcessMode,
    verdict: str,
    status: str | None,
    decision: str,
) -> None:
    # Audit rows must reflect the resolved outcome, including allow, rather
    # than a historical hardcoded block.
    async def completion(**_: object) -> SimpleNamespace:
        return _fake_semantic_response(verdict)

    _stub_eligible(monkeypatch, fake_process_mode)
    install_completion_backend(monkeypatch, completion)
    run = _real_run("drain escalation resolve")

    _persist(run_id=run.id, final_state=_escalation_state(), db_path=isolated_db)

    if status is not None:
        assert _held_status(run.id, isolated_db) == status
    resolved = [
        d
        for d in records.list_safety_decisions(run.id, db_path=isolated_db)
        if d["stage"] == "hypothesis" and "uncertain" not in d["reason"]
    ]
    assert resolved, "the resolution must leave an audit row"
    assert {d["decision"] for d in resolved} == {decision}


def test_rescreen_does_not_downgrade_an_escalation_raised_block(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_process_mode: FakeProcessMode,
) -> None:
    # Re-screening unchanged text must not undo a stronger contextual verdict.

    async def block_completion(**_: object) -> SimpleNamespace:
        return _fake_semantic_response("prohibited")

    _stub_eligible(monkeypatch, fake_process_mode)
    install_completion_backend(monkeypatch, block_completion)
    run = _real_run("drain escalation rescreen")

    _persist(run_id=run.id, final_state=_escalation_state(), db_path=isolated_db)
    assert _held_status(run.id, isolated_db) == "prohibited"
    before = records.list_safety_decisions(run.id, db_path=isolated_db)

    second = screen_hypotheses(
        run.id,
        hypotheses.list_hypotheses(run.id, db_path=isolated_db),
        db_path=isolated_db,
    )

    assert _held_status(run.id, isolated_db) == "prohibited"
    assert "held-1" in second.blocked_ids
    assert second.escalatable == ()
    after = records.list_safety_decisions(run.id, db_path=isolated_db)
    assert len(after) == len(before), "re-screen must not duplicate audit rows"


@pytest.mark.parametrize("failure", ["no_credential", "provider_error"])
def test_drain_escalation_fails_closed(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_process_mode: FakeProcessMode,
    failure: str,
) -> None:
    async def raise_completion(**_: object) -> None:
        raise RuntimeError("provider unavailable")

    if failure == "no_credential":
        monkeypatch.setattr(safety, "_should_escalate_to_semantic", lambda *a, **k: True)
        fake_process_mode.online(credential=False)
    else:
        _stub_eligible(monkeypatch, fake_process_mode)
        install_completion_backend(monkeypatch, raise_completion)
    run = _real_run("drain escalation fails closed")

    _persist(run_id=run.id, final_state=_escalation_state(), db_path=isolated_db)

    assert _held_status(run.id, isolated_db) == "uncertain"


def test_drain_skips_escalation_cleanly_when_offline(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:

    async def fail_if_called(**_: object) -> None:
        raise AssertionError("an offline-backed run must never call the provider")

    install_completion_backend(monkeypatch, fail_if_called)
    run = seed_run("drain escalation offline", llm_backend="offline", db_path=isolated_db)

    drained = _persist(run_id=run.id, final_state=_escalation_state(), db_path=isolated_db)

    assert drained.safety_counts["screened"] == 2
    assert _held_status(run.id, isolated_db) == "uncertain"


def test_escalation_does_not_hold_the_write_lock(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_process_mode: FakeProcessMode,
) -> None:
    # Stall escalation to prove unrelated writes proceed; provider I/O must
    # never hold the SQLite writer.
    call_started = threading.Event()
    release_call = threading.Event()

    async def slow_completion(**_: object) -> SimpleNamespace:
        call_started.set()
        assert release_call.wait(timeout=5), "test did not release the call"
        return _fake_semantic_response("allowed")

    _stub_eligible(monkeypatch, fake_process_mode)
    install_completion_backend(monkeypatch, slow_completion)
    run = _real_run("drain escalation lock check")

    errors: list[BaseException] = []

    def _run_drain() -> None:
        try:
            _persist(
                run_id=run.id,
                final_state=_escalation_state(),
                db_path=isolated_db,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=_run_drain)
    worker.start()
    try:
        assert call_started.wait(timeout=5), "escalation call never started"
        start = time.monotonic()
        seed_run("concurrent write during escalation")
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, (
            "a concurrent write blocked for "
            f"{elapsed:.2f}s -- the write lock is held across the call"
        )
    finally:
        release_call.set()
        worker.join(timeout=5)
    assert not errors, errors


def _final_state_with_article(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "hypotheses": [],
        "articles": [article],
        "tournament_matchups": [],
        "meta_review": {},
        "evolution_details": [],
        "research_overview": {},
    }


def test_drain_persists_evidence_identity_and_retrieval_provenance(
    isolated_db: str,
) -> None:
    run = seed_run("identity goal")
    final_state = _final_state_with_article(
        {
            "title": "A PubMed paper",
            "source": "pubmed",
            "source_id": "12345678",
            "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
            "doi": "10.1000/xyz123",
            "abstract": "An abstract about kinase X.",
            "retrieved_at": 111.5,
            "retrieval_score": 0.87,
            "retrieval_rationale": "Directly addresses the goal.",
            "retriever_version": "hybrid-lexical-semantic/1",
        }
    )
    final_state["articles"].append(
        {
            "title": "Untagged source article",
            "source": "web",
            "url": "https://pubmed.ncbi.nlm.nih.gov/98765432/",
            "abstract": "Some abstract text.",
        }
    )
    _persist_and_finalize(run, final_state, isolated_db)

    by_title = {row["title"]: row for row in records.list_evidence(run.id, db_path=isolated_db)}
    row = by_title["A PubMed paper"]
    assert row["doi"] == "10.1000/xyz123"
    assert row["pmid"] == "12345678"
    assert row["passage_text"] == "A PubMed paper An abstract about kinase X."
    assert row["retrieved_at"] == 111.5
    assert row["created_at"] != row["retrieved_at"]
    assert row["retrieval_score"] == 0.87
    assert row["retrieval_rationale"] == "Directly addresses the goal."
    assert row["retriever_version"] == "hybrid-lexical-semantic/1"
    derived = by_title["Untagged source article"]
    assert derived["pmid"] == "98765432"
    assert derived["doi"] is None
    # Located offsets index the exact materialized passage, never a later
    # reconstruction.
    passages = {p.text for p in evidence_passages(run.id, db_path=isolated_db)}
    assert "A PubMed paper An abstract about kinase X." in passages


def _article(title: str, number: int, **overrides: Any) -> dict[str, Any]:
    return {
        "title": title,
        "source": "pubmed",
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{number}/",
        "abstract": "x",
        **overrides,
    }


def _persisted_evidence(
    isolated_db: str, articles: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    run = seed_run("identity goal")
    state = _final_state_with_article(articles[0])
    state["articles"] = articles
    _persist_and_finalize(run, state, isolated_db)
    return {e["title"]: e for e in records.list_evidence(run.id, db_path=isolated_db)}


def test_offline_resolver_availability_retraction_and_source_type(
    isolated_db: str,
) -> None:
    evidence = _persisted_evidence(
        isolated_db,
        [
            _article("Reachable-by-metadata paper", 1),
            _article("Retracted paper", 2, is_retracted=True),
            _article("No identifier at all", 3, url=""),
            _article("Preprint indexed in pubmed", 6, publication_type="Preprint"),
            {
                **_article("Preprint server paper", 7),
                "source": "biorxiv",
                "url": "https://www.biorxiv.org/content/10.1101/7v1",
            },
        ],
    )

    reachable = evidence["Reachable-by-metadata paper"]
    assert (reachable["available"], reachable["retracted"]) == (True, False)
    assert reachable["source_type"] == "peer_reviewed"
    retracted = evidence["Retracted paper"]
    assert (retracted["available"], retracted["retracted"]) == (False, True)
    unidentified = evidence["No identifier at all"]
    assert (unidentified["available"], unidentified["retracted"]) == (
        False,
        False,
    )
    # Publication type is lost after draining, so it is classified once here.
    assert evidence["Preprint indexed in pubmed"]["source_type"] == "preprint"
    assert evidence["Preprint server paper"]["source_type"] == "preprint"


def test_live_resolver_dereferences_and_persists_retraction(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "evidence_resolver", "live")

    def fake_resolve_many(
        metas: list[CitationMetadata], *, resolver: Resolver
    ) -> list[Resolvability]:
        return [
            Resolvability.RETRACTED if metas[0].retracted else Resolvability.RESOLVABLE,
            Resolvability.UNRESOLVABLE,
            Resolvability.RETRACTED,
        ]

    monkeypatch.setattr(citation_resolver, "resolve_many", fake_resolve_many)

    evidence = _persisted_evidence(
        isolated_db,
        [
            _article("Actually reachable", 1),
            _article("URL present but dead", 2),
            _article("Caught by the live retraction set", 3),
        ],
    )

    reachable = evidence["Actually reachable"]
    assert (reachable["available"], reachable["retracted"]) == (True, False)
    dead = evidence["URL present but dead"]
    assert (dead["available"], dead["retracted"]) == (False, False)
    caught = evidence["Caught by the live retraction set"]
    assert (caught["available"], caught["retracted"]) == (False, True)


_STATEMENT = "Blocking CXCR1 suppresses breast cancer stem cells. It works."


_DERIVED_TITLE = "Blocking CXCR1 suppresses breast cancer stem cells"


@pytest.mark.parametrize(
    ("authored", "expected"),
    [
        ("CXCR1 Blockade Strategy", "CXCR1 Blockade Strategy"),
        (None, _DERIVED_TITLE),
        (["not", "a", "string"], _DERIVED_TITLE),
        (42, _DERIVED_TITLE),
        ("   \n\t", _DERIVED_TITLE),
        ("A" * 150, "A" * 120),
    ],
)
def test_persist_writes_prose_fields_and_the_authored_title_onto_the_row(
    isolated_db: str, authored: Any, expected: str
) -> None:
    # Proposer safety prose is not reviewer assessment and must never control
    # the safety gate, so it is stored as plain prose.
    overrides = {} if authored is None else {"title": authored}
    run = seed_run("CSC goal")
    final_state = {
        "hypotheses": [
            _engine_hypothesis(
                "eng-hyp-title",
                _STATEMENT,
                safety_and_toxicity="Limited human safety data exists.",
                introduction="Breast cancer stem cells drive relapse.",
                recent_findings="CXCR1 is enriched in the stem-like pool.",
                **overrides,
            )
        ],
        "articles": [],
        "tournament_matchups": [],
        "meta_review": {},
        "research_overview": {},
    }
    _persist(run_id=run.id, final_state=final_state, db_path=isolated_db)

    [row] = hypotheses.list_hypotheses(run.id, db_path=isolated_db)
    assert row["title"] == expected
    assert row["safety_and_toxicity"] == "Limited human safety data exists."
    assert row["introduction"] == "Breast cancer stem cells drive relapse."
    assert row["recent_findings"] == "CXCR1 is enriched in the stem-like pool."


def _screening_state() -> dict[str, Any]:
    return {
        "hypotheses": [
            _engine_hypothesis(
                "safe-1",
                "Inhibiting kinase X reduces AML growth via apoptosis.",
            ),
            _engine_hypothesis(
                "unsafe-1",
                "Weaponize the pathogen to enhance transmissibility.",
            ),
        ],
        "articles": [],
        "tournament_matchups": [],
        "meta_review": {},
        "evolution_details": [],
        "research_overview": {},
    }


def test_drain_screens_hypotheses_before_finalize(isolated_db: str) -> None:
    run = seed_run("safety goal")
    state = _screening_state()

    _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    by_text = {h["statement"][:8]: h for h in hypotheses.list_hypotheses(run.id)}
    assert by_text["Inhibiti"]["safety_status"] == "allow"
    assert by_text["Weaponiz"]["safety_status"] == "prohibited"

    decisions = records.list_safety_decisions(run.id, db_path=isolated_db)
    assert any(d["stage"] == "hypothesis" and d["decision"] == "block" for d in decisions)


@pytest.mark.parametrize("engine_audit", [True, False])
def test_drain_persists_held_hypotheses_as_reviewable_decisions(
    isolated_db: str, engine_audit: bool
) -> None:
    # Held ideas leave the engine pool, so drain must preserve adjudicable
    # decisions before they disappear, even when the audit join is missing.
    run = seed_run("held hypotheses goal")
    state = _held_final_state()
    if not engine_audit:
        state["safety_decisions"] = []

    _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    decisions = records.list_safety_decisions(run.id, db_path=isolated_db)
    holds = [d for d in decisions if d["decision"] == "hold"]
    assert len(holds) == 2
    for row in holds:
        assert row["stage"] == "hypothesis"
        assert row["requires_review"] is True
        assert row["resolution"] is None
        assert row["reason"]
    assert [h["id"] for h in hypotheses.list_hypotheses(run.id)] == ["safe-1"]
    if not engine_audit:
        return
    for row in holds:
        assert row["policy_version"] == "coscientist-safety-v5"
        assert row["matches"] == ["for research purposes only"]
        assert "obfuscated intent" in row["reason"]
    reasons = " ".join(row["reason"] for row in holds)
    assert "held-1" in reasons and "held-2" in reasons
    assert "enhance pathogen transmissibility" in reasons
    assert "toxin production line" in reasons


def _multi_parent_state() -> dict[str, Any]:
    return {
        "hypotheses": [
            _engine_hypothesis(
                "parent-1",
                "Parent hypothesis about kinase X.",
                parent_id=None,
                generation=0,
                origin="generation",
            ),
            _engine_hypothesis(
                "parent-2",
                "Parent hypothesis about cofactor W.",
                parent_id=None,
                generation=0,
                origin="generation",
            ),
            _engine_hypothesis(
                "child-1",
                "Combined hypothesis: kinase X with cofactor W.",
                parent_id="parent-1",
                parent_ids=["parent-1", "parent-2"],
                generation=1,
                origin="evolution",
            ),
        ],
        "articles": [],
        "tournament_matchups": [],
        "meta_review": {},
        "evolution_details": [],
        "research_overview": {},
    }


@pytest.mark.parametrize(
    ("pruned", "parent_ids"),
    [(None, ["parent-1", "parent-2"]), ("parent-2", None)],
)
def test_drain_persists_multi_parent_lineage_without_dangling_ids(
    isolated_db: str, pruned: str | None, parent_ids: list[str] | None
) -> None:
    # Archived co-parents must not survive as dangling ids in persisted lineage.
    state = _multi_parent_state()
    state["hypotheses"] = [h for h in state["hypotheses"] if h["id"] != pruned]
    run = seed_run("combine goal")
    _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    hyps = {h["id"]: h for h in hypotheses.list_hypotheses(run.id, db_path=isolated_db)}
    assert hyps["child-1"]["parent_id"] == "parent-1"
    assert hyps["child-1"]["parent_ids"] == parent_ids
    assert hyps["parent-1"]["parent_ids"] is None


def test_drain_persists_creation_iteration(isolated_db: str) -> None:
    # Unknown creation cycles remain NULL so scaling falls back to generation
    # rather than inventing cycle zero.
    run = seed_run("kinase goal")
    _persist(
        run_id=run.id,
        final_state={
            "hypotheses": [
                _engine_hypothesis("seed", "Seed idea.", creation_iteration=0),
                _engine_hypothesis("reborn", "Re-generated later.", creation_iteration=2),
                _engine_hypothesis("legacy", "No cycle stamped."),
            ],
            "articles": [],
            "tournament_matchups": [],
            "meta_review": {},
            "evolution_details": [],
            "research_overview": {},
        },
        db_path=isolated_db,
    )

    by_id = {h["id"]: h for h in hypotheses.list_hypotheses(run.id, db_path=isolated_db)}
    assert by_id["seed"]["creation_iteration"] == 0
    assert by_id["reborn"]["creation_iteration"] == 2
    assert by_id["legacy"]["creation_iteration"] is None


@pytest.mark.parametrize(("iteration", "expected"), [(2, 2), (None, 0)])
def test_persist_match_records_the_iteration_it_was_judged_in(
    isolated_db: str, iteration: int | None, expected: int
) -> None:
    state = _final_state_with_features()
    state["tournament_matchups"][0].pop("iteration", None)
    if iteration is not None:
        state["tournament_matchups"][0]["iteration"] = iteration
    run = seed_run("CSC goal")

    _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    matches = records.list_matches(run.id, db_path=isolated_db)
    assert [match["iteration"] for match in matches] == [expected]


def _judged_matchup() -> dict[str, Any]:
    transcript = [
        {
            "turn": 1,
            "winner": "b",
            "winner_id": "e-b",
            "reasoning": ("Idea 2 names a measurable target. better idea: 2"),
            "presentation_order": "ab",
            "valid_output": True,
        },
        {
            "turn": 2,
            "winner": "b",
            "winner_id": "e-b",
            "reasoning": ("Presented the other way round it still holds. Better idea: 1"),
            "presentation_order": "ba",
            "valid_output": True,
        },
    ]
    response: dict[str, Any] = {"decision_summary": "Idea 2 wins."}
    run = _DebateRun(
        base=_MatchupPrompt("", None),
        transcript=transcript,
        fallback="a",
        start_parity=0,
    )
    winner = _finalize_debate_response(response, ["b", "b"], run, "model")
    pair = (Hypothesis("A", id="e-a"), Hypothesis("B", id="e-b"))
    matchup: dict[str, Any] = build_matchup(
        pair, winner, response, k_factor=None, iteration=0
    ).to_dict()
    return matchup


def test_a_judged_debate_reaches_the_report(isolated_db: str) -> None:
    # Each debate turn may swap presentation numbers; persist one canonical
    # match verdict.
    run = seed_run("cardiac fibrosis goal", profile="express")
    with db.transaction(isolated_db) as conn:
        _persist_engine_matches(
            run.id,
            [_judged_matchup()],
            {"e-a": "h1", "e-b": "h2"},
            conn,
        )

    [row] = records.list_matches(run.id, db_path=isolated_db)
    document = json.loads(row["debate_transcript"])
    assert document["verdict"] == "2"
    assert [turn["favored"] for turn in document["turns"]] == ["2", "2"]
    assert document["turns"][0]["text"] == "Idea 2 names a measurable target."
    assert document["turns"][1]["text"].endswith("it still holds.")

    markdown = report_markdown.render_report_markdown(
        report_markdown.ReportMarkdownInputs(
            research_goal="cardiac fibrosis goal",
            provider="engine",
            top_hypotheses=[{"id": "h2", "title": "Empagliflozin"}],
            matches=records.list_matches(run.id, db_path=isolated_db),
            hypothesis_title_by_id={"h1": "NHE1 screen", "h2": "Empagliflozin"},
        )
    )

    assert "## Tournament debates" in markdown
    assert "### Debate 1: 1. NHE1 screen vs 2. Empagliflozin" in markdown
    assert (
        '**Turn 1 (favors idea 2; this turn\'s "Hypothesis 1" is idea 1):**'
        " Idea 2 names a measurable target." in markdown
    )
    # Swapped turn headers explain the numbering used by their own argument.
    assert '**Turn 2 (favors idea 2; this turn\'s "Hypothesis 1" is idea 2):**' in markdown
    assert markdown.rstrip().count("Better idea:") == 1
    assert "Better idea: 2" in markdown


@pytest.mark.parametrize(
    ("novelties", "expected"),
    [([3.0], 3.0), ([2.0, 4.0], 3.0), ([None], None), ([], None)],
)
def test_novelty_score_comes_from_the_reviewers_novelty_axis(
    isolated_db: str, novelties: list[float | None], expected: float | None
) -> None:
    # Overall score deliberately differs from novelty so incorrect column
    # mapping cannot pass by coincidence.
    reviews = []
    for novelty in novelties:
        scores: dict[str, Any] = {"scientific_soundness": 9}
        if novelty is not None:
            scores["novelty"] = novelty
        reviews.append(
            {
                "scores": scores,
                "overall_score": 9.0,
                "review_summary": "s",
                "constructive_feedback": "c",
            }
        )
    state = {
        "hypotheses": [_engine_hypothesis("h-1", "A hypothesis.", score=9.0, reviews=reviews)],
        "articles": [],
        "tournament_matchups": [],
        "meta_review": {},
        "evolution_details": [],
        "research_overview": {},
    }
    run = seed_run("novelty goal")

    _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    [row] = hypotheses.list_hypotheses(run.id, db_path=isolated_db)
    assert row["novelty_score"] == expected
