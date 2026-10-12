from __future__ import annotations

from typing import Any

import litellm
import pytest
from co_scientist.domains.research_state.claims import (
    AssessorDraft,
    EntailmentLabel,
    EvidencePassage,
    as_passages,
    assess_claim,
    assess_claims_batch,
)
from co_scientist.domains.research_state.claims.grounding import (
    AssessorSpec,
    ClaimRecord,
    GroundingResult,
    assess_hypothesis_claims,
    build_assessor,
    claim_fingerprint,
    evidence_passages,
    persist_grounding,
    reusable_assessments,
)
from co_scientist.domains.research_state.claims.verifier import (
    make_llm_assessor,
    make_llm_batch_assessor,
)
from co_scientist.domains.research_state.models import ExecutionMetrics
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.domains.research_state.repository import records as store
from co_scientist.domains.research_state.repository.hypotheses import NewHypothesis
from co_scientist.domains.research_state.repository.records import NewEvidence
from co_scientist.orchestration.engine_tasks import gate as engine_tasks_gate
from co_scientist.platform.llm import scoped_telemetry

from tests._drain_helpers import _build_report
from tests._store_helpers import _add, seed_run

from ._llm_fake_backend import completion_response, install_completion_backend

_KINASE_PASSAGE = EvidencePassage(
    evidence_id="one",
    text="Kinase X inhibition reduces tumor growth in AML cell lines.",
)


@pytest.mark.parametrize("failure", [True, False])
def test_claim_provider_records_only_deterministic_substitution(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    async def provider(**kwargs: Any) -> Any:
        if failure:
            raise RuntimeError("offline test provider failure")
        return litellm.ModelResponse(
            model="deepseek/deepseek-chat",
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            '{"label":"supports","supporting":[{"passage":1,'
                            '"quote":"reduces tumor growth"}],'
                            '"contradicting":[]}'
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
        )

    install_completion_backend(monkeypatch, provider)
    assessor, identity = make_llm_assessor("deepseek/deepseek-chat")
    with scoped_telemetry("claims") as usage:
        result = assess_claim(
            "Kinase X inhibition reduces tumor growth.",
            [_KINASE_PASSAGE],
            assessor=assessor,
            assessor_id=identity,
        )
    assert result.label is EntailmentLabel.SUPPORTS
    row = usage.snapshot()["claims::deepseek/deepseek-chat"]
    assert row["deterministic_fallbacks"] == ({"claim_single": 1} if failure else {})


@pytest.mark.parametrize("has_evidence", [True, False])
def test_batch_fallback_counts_claims_only_after_judging(
    monkeypatch: pytest.MonkeyPatch, has_evidence: bool
) -> None:
    async def unavailable(**kwargs: Any) -> Any:
        raise RuntimeError("offline test provider failure")

    install_completion_backend(monkeypatch, unavailable)
    assessor, identity = make_llm_batch_assessor("deepseek/deepseek-chat")
    with scoped_telemetry("claims") as usage:
        assess_claims_batch(
            ["Kinase X inhibition reduces tumor growth."],
            [_KINASE_PASSAGE] if has_evidence else [],
            batch_assessor=assessor,
            assessor_id=identity,
        )
    if has_evidence:
        row = usage.snapshot()["claims::deepseek/deepseek-chat"]
        assert row["deterministic_fallbacks"] == {"claim_batch": 1}
    else:
        assert usage.snapshot() == {}


@pytest.mark.parametrize("calls", [25, 0])
def test_gate_telemetry_is_folded_into_the_run_metrics(calls: int) -> None:
    state: dict[str, Any] = {"metrics": ExecutionMetrics(llm_calls=7)}
    usage = {"claim_gate::llm:test-model": {"calls": 25, "prompt_tokens": 100}} if calls else {}

    engine_tasks_gate._fold_gate_telemetry(state, usage)

    assert state["metrics"].llm_calls == 7 + calls
    assert bool(state["metrics"].model_usage) is bool(calls)


# Verdicts depend on the claim and retrieved passages; unrelated arrivals must
# not invalidate reuse.

_CLAIM = "Inhibiting kinase X reduces melanoma tumor growth in mouse models."
_OTHER = "A dietary change improves cardiovascular outcomes in adults."
_RELEVANT = (
    "In mouse models, inhibiting kinase X reduced melanoma tumor growth "
    "substantially across every cohort."
)
_UNRELATED = (
    "Sediment cores from the Baltic show a shift in diatom assemblages during the mid-Holocene."
)


def _gate_record(
    claims: list[tuple[str, str, str]], assessor_id: str = "test-v1"
) -> dict[str, Any]:
    return {
        "assessor": assessor_id,
        "claims": [
            {
                "claim": claim,
                "role": role,
                "fingerprint": fingerprint,
                "label": EntailmentLabel.SUPPORTS.value,
                "supporting_passages": [],
                "contradicting_passages": [],
            }
            for claim, role, fingerprint in claims
        ],
    }


def test_a_claim_fingerprint_tracks_only_its_own_evidence_and_assessor() -> None:
    record = ClaimRecord(_CLAIM, "speculative")
    relevant = as_passages([_RELEVANT])

    base = claim_fingerprint(record, relevant, "test-v1")

    unrelated = as_passages([_RELEVANT, _UNRELATED])
    assert claim_fingerprint(record, unrelated, "test-v1") == base
    assert claim_fingerprint(record, as_passages([]), "test-v1") != base
    assert claim_fingerprint(record, relevant, "other-v2") != base

    identifier_claim = ClaimRecord("Protein H folds cooperatively.", "x")
    empty = claim_fingerprint(identifier_claim, [], "llm:test")
    new_evidence = EvidencePassage(
        "protein-study",
        "H adopts native structure through a concerted transition.",
    )
    assert claim_fingerprint(identifier_claim, [new_evidence], "llm:test") != empty


def test_a_stored_verdict_is_reused_only_when_its_fingerprint_matches() -> None:
    passages = as_passages([_RELEVANT])
    seen: list[str] = []

    def assessor(claim: str, _passages: Any) -> AssessorDraft:
        seen.append(claim)
        return AssessorDraft(EntailmentLabel.INSUFFICIENT)

    fingerprint = claim_fingerprint(ClaimRecord(_OTHER, "categorical"), passages, "test-v1")
    hypothesis = {"id": "h1", "statement": _CLAIM, "mechanism": _OTHER}
    spec = AssessorSpec(assessor, "test-v1")

    reused = assess_hypothesis_claims(
        [hypothesis],
        passages,
        spec,
        reuse={"h1": reusable_assessments(_gate_record([(_OTHER, "categorical", fingerprint)]))},
    )
    assert seen == [_CLAIM]
    pairs = [(a.claim, role) for a, role in reused[0][1]]
    assert pairs == [(_CLAIM, "speculative"), (_OTHER, "categorical")]
    assert reused[0][1][1][0].label is EntailmentLabel.SUPPORTS

    seen.clear()
    assess_hypothesis_claims([hypothesis], passages, spec, reuse={})
    assert sorted(seen) == sorted([_CLAIM, _OTHER])
    assert reusable_assessments(_gate_record([(_CLAIM, "speculative", "")])) == {}


def _supporting_gate_record(fingerprint: str, span: dict[str, Any]) -> dict[str, Any]:
    record = _gate_record([(_CLAIM, "speculative", fingerprint)])
    record["claims"][0]["supporting_passages"] = [span]
    return record


@pytest.mark.parametrize(
    ("span_url", "reused"),
    [("https://example.org/kinase", True), ("https://example.org/elsewhere", False)],
)
def test_finalize_reuses_a_gate_verdict_with_spans_on_its_own_evidence_ids(
    span_url: str, reused: bool
) -> None:
    url = "https://example.org/kinase"
    gate_passage = EvidencePassage("pmid-1", _RELEVANT, source="pubmed", url=url)
    store_passage = EvidencePassage("row-7f3a", _RELEVANT, source="pubmed", url=url)
    quote = _RELEVANT[:40]
    span = {"evidence_id": "pmid-1", "quote": quote, "start": 0, "end": 40}
    span |= {"source": "pubmed", "url": span_url}
    fingerprint = claim_fingerprint(ClaimRecord(_CLAIM, "speculative"), [gate_passage], "test-v1")
    seen: list[str] = []

    def assessor(claim: str, _passages: Any) -> AssessorDraft:
        seen.append(claim)
        return AssessorDraft(EntailmentLabel.INSUFFICIENT)

    result = assess_hypothesis_claims(
        [{"id": "h1", "statement": _CLAIM}],
        [store_passage],
        AssessorSpec(assessor, "test-v1"),
        reuse={"h1": reusable_assessments(_supporting_gate_record(fingerprint, span))},
    )

    assessment = result[0][1][0][0]
    assert seen == ([] if reused else [_CLAIM])
    if reused:
        assert assessment.label is EntailmentLabel.SUPPORTS
        assert [s.evidence_id for s in assessment.supporting_passages] == ["row-7f3a"]
        assert assessment.supporting_passages[0].quote == quote


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("model_opposition_verified", "model_opposition_verified"),
        (None, "legacy_unknown"),
    ],
)
def test_a_reused_verdict_keeps_its_verification_method(method: str | None, expected: str) -> None:
    record = _gate_record([(_CLAIM, "speculative", "fixed-fingerprint")])
    if method:
        record["claims"][0]["verification_method"] = method

    restored = reusable_assessments(record)

    assert restored["fixed-fingerprint"].verification_method == expected


# Hypotheses persist their claim graph; the gate decides publication from it.

_CONTRADICTED = "Inhibiting kinase X reduces melanoma tumor growth in mouse models."
_CONTRADICTING_EVIDENCE = (
    "In mouse models, inhibiting kinase X did not reduce melanoma tumor "
    "growth; there was no significant effect on tumor growth."
)
_SUPPORTED = "A dietary change improves cardiovascular outcomes in adults."
_NO_CLAIM = "Kinase X trial."


def _add_categorical(run_id: str, title: str, claim: str, db: str) -> str:
    # Mechanism carries established-fact claims; the fixture must state the role
    # that controls blocking.
    return _add(run_id, title, _NO_CLAIM, db, mechanism=claim)


def test_ground_persists_graph_and_blocks_contradicted(
    isolated_db: str,
) -> None:
    run = seed_run("grounding goal", provider="mock")
    bad_id = _add_categorical(run.id, "Contradicted", _CONTRADICTED, isolated_db)
    ok_id = _add(run.id, "Benign", _SUPPORTED, isolated_db)

    result = persist_grounding(
        run.id,
        assess_hypothesis_claims(
            hypotheses.list_hypotheses(run.id),
            as_passages([_CONTRADICTING_EVIDENCE]),
        ),
        db_path=isolated_db,
    )

    assert isinstance(result, GroundingResult)
    assert result.blocked_ids == frozenset({bad_id, ok_id})
    edges = store.list_claim_evidence(run.id, db_path=isolated_db)
    contradicted = next(e for e in edges if e["hypothesis_id"] == bad_id)
    assert contradicted["label"] == "contradicts"
    assert contradicted["claim_role"] == "categorical"
    assert contradicted["assessor"]
    span = contradicted["contradicting"][0]
    assert span["evidence_id"] == "passage-0"
    assert span["quote"] and span["end"] > span["start"] >= 0
    speculative = next(e for e in edges if e["hypothesis_id"] == ok_id)
    assert speculative["label"] == "insufficient"
    assert speculative["claim_role"] == "speculative"
    decisions = store.list_safety_decisions(run.id, db_path=isolated_db)
    assert any(d["stage"] == "claim_gate" and d["decision"] == "block" for d in decisions)


def test_unsupported_categorical_rationale_is_quarantined(
    isolated_db: str,
) -> None:
    run = seed_run("grounding goal")
    hypothesis_id = hypotheses.add_hypothesis(
        NewHypothesis(
            run_id=run.id,
            title="Unsupported rationale",
            statement="We hypothesize kinase X may alter neuronal recovery.",
            mechanism="Kinase X is established as the recovery controller.",
        ),
        db_path=isolated_db,
    )

    result = persist_grounding(
        run.id,
        assess_hypothesis_claims(
            hypotheses.list_hypotheses(run.id, db_path=isolated_db),
            as_passages(["An unrelated passage about photosynthesis in plants."]),
        ),
        db_path=isolated_db,
    )

    assert result.blocked_ids == frozenset({hypothesis_id})
    edges = store.list_claim_evidence(run.id, db_path=isolated_db)
    by_role = {edge["claim_role"]: edge for edge in edges}
    assert by_role["speculative"]["label"] == "insufficient"
    assert by_role["categorical"]["label"] == "insufficient"


def _seed_contradiction_report_run(
    db_path: str, bad_claim_is_categorical: bool
) -> tuple[Any, str, str]:
    # A contradicted established fact withholds the idea; a contradicted
    # proposal remains publishable.
    run = seed_run("grounding goal")
    bad_id = (
        _add_categorical(run.id, "Contradicted", _CONTRADICTED, db_path)
        if bad_claim_is_categorical
        else _add(run.id, "Contradicted", _CONTRADICTED, db_path)
    )
    ok_id = _add(run.id, "Benign", _SUPPORTED, db_path)
    for title, abstract in (
        ("Kinase X mouse study", _CONTRADICTING_EVIDENCE),
        ("Cardiovascular diet study", _SUPPORTED),
    ):
        store.add_evidence(
            NewEvidence(run_id=run.id, title=title, abstract=abstract),
            db_path=db_path,
        )
    persist_grounding(
        run.id,
        assess_hypothesis_claims(
            hypotheses.list_hypotheses(run.id),
            evidence_passages(run.id, db_path=db_path),
        ),
        db_path=db_path,
    )
    return run, bad_id, ok_id


@pytest.mark.parametrize(("categorical", "bad_reaches_the_report"), [(True, False), (False, True)])
async def test_only_a_contradicted_established_fact_is_withheld_from_the_report(
    isolated_db: str, categorical: bool, bad_reaches_the_report: bool
) -> None:
    # Evidence against a proposal is a finding owed to the reader, not a reason
    # to hide the proposal.
    run, bad_id, ok_id = _seed_contradiction_report_run(
        isolated_db, bad_claim_is_categorical=categorical
    )

    payload, markdown = await _build_report(run, isolated_db)

    leaderboard_ids = {row["id"] for row in payload["leaderboard"]}
    assert (bad_id in leaderboard_ids) is bad_reaches_the_report
    assert ok_id in leaderboard_ids
    if not bad_reaches_the_report:
        assert "kinase X reduces melanoma" not in markdown


async def test_speculative_insufficient_hypothesis_remains_visible(
    isolated_db: str,
) -> None:
    run = seed_run("novel proposal")
    hypothesis_id = hypotheses.add_hypothesis(
        NewHypothesis(
            run_id=run.id,
            title="Novel proposal",
            statement=("We hypothesize channel X may alter neuronal ATP recovery."),
            mechanism="Astrocytes contribute to neuronal energy metabolism.",
        ),
        db_path=isolated_db,
    )
    store.add_evidence(
        NewEvidence(
            run_id=run.id,
            title="General energetics review",
            abstract="Astrocytes contribute to neuronal energy metabolism.",
        ),
        db_path=isolated_db,
    )
    persist_grounding(
        run.id,
        assess_hypothesis_claims(
            hypotheses.list_hypotheses(run.id, db_path=isolated_db),
            evidence_passages(run.id, db_path=isolated_db),
        ),
        db_path=isolated_db,
    )

    payload, markdown = await _build_report(run, isolated_db)

    assert hypothesis_id in {row["id"] for row in payload["leaderboard"]}
    edge = next(
        edge
        for edge in store.list_claim_evidence(run.id, db_path=isolated_db)
        if edge["claim_role"] == "speculative"
    )
    assert edge["label"] == "insufficient"
    report_edges = payload["claim_evidence"]
    assert {item["hypothesis_id"] for item in report_edges} == {hypothesis_id}
    supported = next(item for item in report_edges if item["label"] == "supports")
    assert supported["supporting"][0]["source_title"] == ("General energetics review")
    assert "**Supported · categorical**" in markdown
    assert "**Speculative — evidence insufficient · speculative**" in markdown


def test_evidence_passages_excludes_unavailable_sources(
    isolated_db: str,
) -> None:
    run = seed_run("grounding goal")
    current_id = store.add_evidence(
        NewEvidence(
            run_id=run.id,
            title="Current publication",
            url="https://example.org/current",
            abstract="A current result supports the proposed mechanism.",
            available=True,
        ),
        db_path=isolated_db,
    )
    store.add_evidence(
        NewEvidence(
            run_id=run.id,
            title="Retracted publication",
            url="https://example.org/retracted",
            abstract="A retracted result must not support the mechanism.",
            available=False,
        ),
        db_path=isolated_db,
    )

    passages = evidence_passages(run.id, db_path=isolated_db)

    assert [passage.evidence_id for passage in passages] == [current_id]


@pytest.mark.parametrize(
    ("force_offline", "expected"),
    [
        (False, "llm:deepseek/deepseek-chat"),
        (True, "deterministic-v1"),
    ],
)
def test_offline_never_builds_the_assessor_that_calls_a_provider(
    monkeypatch: pytest.MonkeyPatch,
    force_offline: bool,
    expected: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-not-called-by-this-test")
    if force_offline:
        monkeypatch.setenv("COSCIENTIST_TEST_DOUBLE", "deterministic")
    else:
        monkeypatch.delenv("COSCIENTIST_TEST_DOUBLE", raising=False)

    _, assessor_id = build_assessor("deepseek/deepseek-chat")

    assert assessor_id == expected


def test_ground_with_llm_assessor_persists_provenance(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COSCIENTIST_TEST_DOUBLE", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-not-called-by-this-test")
    run = seed_run("grounding goal")
    hyp_id = _add(run.id, "Supported", _CONTRADICTED, isolated_db)
    ev_id = store.add_evidence(
        NewEvidence(
            run_id=run.id,
            title="Kinase X melanoma study",
            source="pubmed",
            url="https://example.org/ev",
            abstract=("Kinase X inhibition reduces melanoma tumor growth markedly."),
        ),
        db_path=isolated_db,
    )

    async def completion(**_kwargs: Any) -> Any:
        # Legacy evidence-id citations remain accepted even though prompts
        # request passage numbers.
        return completion_response(
            '{"label": "supports", "supporting": '
            f'[{{"passage": "{ev_id}", '
            '"quote": "reduces melanoma tumor growth"}], '
            '"contradicting": []}'
        )

    install_completion_backend(monkeypatch, completion)
    assessor, assessor_id = build_assessor("deepseek/deepseek-chat")
    persist_grounding(
        run.id,
        assess_hypothesis_claims(
            hypotheses.list_hypotheses(run.id),
            evidence_passages(run.id, db_path=isolated_db),
            AssessorSpec(assessor, assessor_id),
        ),
        db_path=isolated_db,
    )

    edges = store.list_claim_evidence(run.id, db_path=isolated_db)
    edge = next(e for e in edges if e["hypothesis_id"] == hyp_id)
    assert edge["assessor"] == "llm:deepseek/deepseek-chat"
    span = edge["supporting"][0]
    assert span["evidence_id"] == ev_id
    assert span["quote"] == "reduces melanoma tumor growth"
    assert span["url"] == "https://example.org/ev"


def test_claim_assessment_holds_no_database_connection(
    isolated_db: str,
) -> None:
    # Provider assessment must precede persistence so SQLite write locks never
    # span network I/O.
    import sqlite3

    hyp = {"id": "h1", "title": "Kinase X inhibition", "statement": _SUPPORTED}

    blocker = sqlite3.connect(isolated_db, timeout=0.5, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        assessed = assess_hypothesis_claims([hyp], as_passages([_SUPPORTED]))
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert [hyp_id for hyp_id, _ in assessed] == ["h1"]
    assert all(claims for _, claims in assessed)


def test_claim_assessment_runs_concurrently(isolated_db: str) -> None:
    # Independent assessments can overlap; serial provider calls make
    # finalization unboundedly slow.
    import threading
    import time as _time

    from co_scientist.domains.research_state.claims.grounding import assess_hypothesis_claims

    active = 0
    peak = 0
    lock = threading.Lock()

    def _slow_assessor(claim: str, passages: Any) -> Any:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        _time.sleep(0.05)
        with lock:
            active -= 1
        return AssessorDraft(label=EntailmentLabel.INSUFFICIENT)

    # Distinct claims: ideas stating the same claim share one check.
    hyps = [
        {"id": f"h{i}", "title": f"H{i}", "statement": f"{_SUPPORTED[:-1]} in cohort {i}."}
        for i in range(8)
    ]
    from co_scientist.domains.research_state.claims.grounding import AssessorSpec

    assess_hypothesis_claims(hyps, as_passages([_SUPPORTED]), AssessorSpec(_slow_assessor))

    assert peak > 1, f"claims were assessed serially (peak concurrency {peak})"


@pytest.mark.parametrize(("hypothesis_count", "claims_each", "calls"), [(13, 17, 13), (1, 25, 2)])
def test_batch_assessor_costs_one_call_per_hypothesis_and_splits_dense_ones(
    hypothesis_count: int, claims_each: int, calls: int
) -> None:
    seen: list[int] = []

    def batch(claims: Any, passages: Any) -> Any:
        seen.append(len(claims))
        return [AssessorDraft(label=EntailmentLabel.INSUFFICIENT)] * len(claims)

    def statement(h: int) -> str:
        return " ".join(
            f"Claim number {i} about a dietary change improving outcomes in study {h}."
            for i in range(claims_each)
        )

    hyps = [
        {"id": f"h{i}", "title": f"H{i}", "statement": statement(i)}
        for i in range(hypothesis_count)
    ]

    assessed = assess_hypothesis_claims(
        hyps,
        as_passages([_SUPPORTED]),
        AssessorSpec(assessor_id="llm:test-model", batch_assessor=batch),
    )

    assert len(seen) == calls
    assert [len(claims) for _, claims in assessed] == [claims_each] * hypothesis_count
