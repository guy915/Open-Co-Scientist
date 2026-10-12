from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from co_scientist.core.exceptions import (
    LLMCallBudgetExceededError,
    LLMRateLimitParkError,
)
from co_scientist.domains.research_state.claims import (
    ClaimAssessment,
    EntailmentLabel,
    EvidencePassage,
    assess_claim,
    assess_claims_batch,
)
from co_scientist.domains.research_state.claims.chunking import chunk_evidence_passage
from co_scientist.domains.research_state.claims.verifier import (
    make_llm_assessor,
    make_llm_batch_assessor,
)
from co_scientist.platform.llm import scoped_telemetry
from litellm.exceptions import RateLimitError

from ._llm_fake_backend import (
    completion_response,
    fake_completion,
    install_completion_backend,
)

_MODEL = "deepseek/deepseek-chat"
_CLAIM = "Kinase X inhibition reduces tumor growth."
_PASSAGE = EvidencePassage(
    evidence_id="ev-1",
    text="Kinase X inhibition reduces tumor growth in AML cell lines.",
    source="pubmed",
    url="https://example.org/1",
)
_OTHER = EvidencePassage(
    evidence_id="ev-0",
    text="Kinase X inhibition reduces tumor growth in a different model.",
)
_CHUNKS = chunk_evidence_passage(
    "article-1",
    head_text="",
    body_text=(
        "Unrelated filler sentence about something else entirely. "
        "Kinase X inhibition reduces tumor growth in AML cell lines. "
    )
    * 40,
    source="pubmed",
    url="https://example.org/article-1",
)
_CHUNK = next(p for p in _CHUNKS if "reduces tumor growth" in p.text)

_MODES = pytest.mark.parametrize("mode", ["single", "batch"])


def _verdict(label: str, supporting: Any = (), contradicting: Any = ()) -> dict[str, Any]:
    return {
        "label": label,
        "supporting": supporting,
        "contradicting": contradicting,
    }


def _cite(passage: Any, quote: str) -> dict[str, Any]:
    return {"passage": passage, "quote": quote}


def _reply(mode: str, verdict: dict[str, Any]) -> str:
    body = verdict if mode == "single" else {"verdicts": [{"index": 1, **verdict}]}
    return json.dumps(body)


def _assess(mode: str, passages: list[EvidencePassage], claim: str = _CLAIM) -> ClaimAssessment:
    if mode == "single":
        assessor, assessor_id = make_llm_assessor(_MODEL)
        return assess_claim(claim, passages, assessor=assessor, assessor_id=assessor_id)
    batch, assessor_id = make_llm_batch_assessor(_MODEL)
    return assess_claims_batch([claim], passages, batch_assessor=batch, assessor_id=assessor_id)[0]


@_MODES
@pytest.mark.parametrize(
    ("label", "supporting", "passages", "expected", "evidence_id"),
    [
        pytest.param(
            "supports",
            [_cite(1, "reduces tumor growth")],
            [_PASSAGE],
            EntailmentLabel.SUPPORTS,
            "ev-1",
            id="supports-located",
        ),
        pytest.param(
            "partial",
            [_cite(1, "reduces tumor growth")],
            [_PASSAGE],
            EntailmentLabel.PARTIAL,
            "ev-1",
            id="partial-located",
        ),
        pytest.param(
            "supports",
            _cite(1, "reduces tumor growth"),
            [_PASSAGE],
            EntailmentLabel.SUPPORTS,
            "ev-1",
            id="single-object-not-a-list",
        ),
        pytest.param(
            "supports",
            [_cite("2", "reduces tumor growth")],
            [_OTHER, _PASSAGE],
            EntailmentLabel.SUPPORTS,
            "ev-1",
            id="cited-by-passage-number",
        ),
        pytest.param(
            "supports",
            [_cite(_CHUNK.evidence_id, "reduces tumor growth in AML")],
            _CHUNKS,
            EntailmentLabel.SUPPORTS,
            _CHUNK.evidence_id,
            id="cited-by-chunk-id",
        ),
        pytest.param(
            "supports",
            [_cite(1, "cures every disease")],
            [_PASSAGE],
            EntailmentLabel.INSUFFICIENT,
            None,
            id="hallucinated-quote",
        ),
        pytest.param(
            "supports",
            [_cite(1, "in AML cell lines")],
            [_OTHER, _PASSAGE],
            EntailmentLabel.INSUFFICIENT,
            None,
            id="quote-from-a-different-passage",
        ),
        pytest.param(
            "supports",
            [_cite(9, "cures every disease")],
            [_PASSAGE],
            EntailmentLabel.INSUFFICIENT,
            None,
            id="out-of-range-passage-number",
        ),
    ],
)
def test_a_verdict_keeps_only_support_located_in_the_shown_passages(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    label: str,
    supporting: Any,
    passages: list[EvidencePassage],
    expected: EntailmentLabel,
    evidence_id: str | None,
) -> None:
    install_completion_backend(
        monkeypatch, fake_completion(_reply(mode, _verdict(label, supporting)))
    )
    with caplog.at_level(logging.INFO, logger="co_scientist.domains.research_state.claims"):
        result = _assess(mode, passages)

    assert result.label is expected
    if evidence_id is None:
        assert result.supporting_passages == ()
        assert "could not be located" in caplog.text
        return
    assert result.verification_method == "model_primary"
    span = result.supporting_passages[0]
    assert span.evidence_id == evidence_id
    shown = next(p for p in passages if p.evidence_id == evidence_id)
    assert shown.text[span.start : span.end] == span.quote


@_MODES
def test_a_provider_error_falls_back_to_the_deterministic_verdict(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    async def down(**_kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    install_completion_backend(monkeypatch, down)
    result = _assess(mode, [_PASSAGE], "Kinase X inhibition reduces tumor growth in AML.")

    assert result.label is EntailmentLabel.SUPPORTS
    assert result.verification_method == "deterministic_lexical"
    assert result.assessor == f"llm:{_MODEL}"
    assert result.supporting_passages


@_MODES
def test_an_answerless_first_attempt_is_asked_again(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    replies = [
        "not valid json",
        _reply(mode, _verdict("supports", [_cite(1, "reduces tumor growth")])),
    ]
    requests: list[Any] = []

    async def flaky(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return completion_response(replies[len(requests) - 1])

    install_completion_backend(monkeypatch, flaky)
    result = _assess(mode, [_PASSAGE])

    assert len(requests) == 2
    assert result.label is EntailmentLabel.SUPPORTS
    assert result.verification_method == "model_primary"


@_MODES
def test_the_entailment_request_names_the_model_and_disables_thinking(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Evidence precedes the claim so provider caching sees a stable prefix.
    seen: dict[str, Any] = {}

    async def capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return completion_response(_reply(mode, _verdict("insufficient")))

    install_completion_backend(monkeypatch, capture)
    if mode == "single":
        assessor, _ = make_llm_assessor("deepseek/deepseek-v4-flash")
        assessor("some claim", [_PASSAGE])
    else:
        batch, _ = make_llm_batch_assessor("deepseek/deepseek-v4-flash")
        batch(["some claim"], [_PASSAGE])

    assert seen["model"] == "deepseek/deepseek-v4-flash"
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}
    prompt = json.dumps(seen["messages"])
    assert prompt.index("EVIDENCE:") < prompt.rindex("CLAIM")


def test_without_evidence_the_provider_is_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(**_kwargs: Any) -> Any:
        raise AssertionError("the provider must not be called")

    install_completion_backend(monkeypatch, forbidden)
    counter = [0]
    batch, _ = make_llm_batch_assessor(_MODEL, call_counter=counter)
    single, _ = make_llm_assessor(_MODEL)

    draft = single("some claim", [])
    assert draft.label is EntailmentLabel.INSUFFICIENT
    assert draft.verification_method == "no_evidence"
    assess_claims_batch(["some claim"], [], batch_assessor=batch, assessor_id="llm:m")
    assert counter[0] == 0


def test_batch_verdicts_map_back_to_claims_by_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_completion_backend(
        monkeypatch,
        fake_completion(
            json.dumps(
                {
                    "verdicts": [
                        {
                            "index": 2,
                            **_verdict("supports", [_cite(1, "reduces tumor growth")]),
                        },
                        {"index": 1, **_verdict("insufficient")},
                    ]
                }
            )
        ),
    )
    batch, assessor_id = make_llm_batch_assessor(_MODEL)
    results = assess_claims_batch(
        ["Some unrelated claim.", _CLAIM],
        [_PASSAGE],
        batch_assessor=batch,
        assessor_id=assessor_id,
    )

    assert assessor_id == make_llm_assessor(_MODEL)[1] == f"llm:{_MODEL}"
    assert results[0].label is EntailmentLabel.INSUFFICIENT
    assert results[1].label is EntailmentLabel.SUPPORTS
    assert results[1].verification_method == "model_primary"


def _zero_price_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from co_scientist.platform.llm.admission import free_policy as free_catalog

    monkeypatch.setattr(
        free_catalog,
        "_reader",
        free_catalog.CatalogReader(
            lambda: {
                model: {
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                    },
                }
                for model in (
                    "nvidia/nemotron-3-ultra-550b-a55b:free",
                    "nvidia/nemotron-3-super-120b-a12b:free",
                )
            }
        ),
    )


@pytest.mark.parametrize("reply", ['{"verdests": []}', '{"verdicts": []}'])
def test_an_empty_batch_reply_falls_back_without_retry_on_a_zero_price_route(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    _zero_price_catalog(monkeypatch)
    requests: list[dict[str, Any]] = []

    async def empty(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return completion_response(reply)

    install_completion_backend(monkeypatch, empty)
    model = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
    batch, assessor_id = make_llm_batch_assessor(model)
    (result,) = assess_claims_batch(
        [_CLAIM], [_PASSAGE], batch_assessor=batch, assessor_id=assessor_id
    )

    assert len(requests) == 1
    assert result.label is EntailmentLabel.SUPPORTS
    assert result.verification_method == "deterministic_lexical"
    request = requests[0]
    assert request["model"] == model
    assert request["api_base"] == "https://openrouter.ai/api/v1"
    provider = request["extra_body"]["provider"]
    assert provider["max_price"] == {"prompt": 0, "completion": 0, "request": 0}
    assert provider["require_parameters"] is True


_OFF_TARGET = (
    "Donepezil, at clinically achievable human brain free concentrations of "
    "10-30 nM, occupies sigma-1R (Ki ~14 nM) at the ER-mitochondria contact "
    "site.",
    "The same ligand is ineffective at blocking wild-type NaVs and does not "
    "disrupt action potential signals in neuronal cells or brain tissue at "
    "working concentrations.",
)
_CONFIRMATORY = (
    "GBM cells engage compensatory stress-response programs "
    "(autophagy-glycolysis crosstalk) to survive metabolic stress.",
    "These findings identify a novel and complex compensatory interplay "
    "between glycolysis, autophagy, and senescence that helps maintain "
    "stemness in heterogeneous GBM tumor subpopulations.",
)
_NEGATION = (
    "Kinase X inhibition reduces tumor growth in AML cells.",
    "Kinase X inhibition did not reduce tumor growth in AML cells.",
)


@_MODES
@pytest.mark.parametrize(
    ("claim", "quote", "expected"),
    [
        (*_OFF_TARGET, EntailmentLabel.INSUFFICIENT),
        (*_CONFIRMATORY, EntailmentLabel.INSUFFICIENT),
        (*_NEGATION, EntailmentLabel.CONTRADICTS),
    ],
    ids=["off-target-quote", "confirmatory-quote", "genuine-negation"],
)
def test_a_contradiction_needs_a_quote_that_negates_the_claim(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    claim: str,
    quote: str,
    expected: EntailmentLabel,
) -> None:
    install_completion_backend(
        monkeypatch,
        fake_completion(_reply(mode, _verdict("contradicts", contradicting=[_cite(1, quote)]))),
    )
    result = _assess(mode, [EvidencePassage("ev-1", quote)], claim)

    assert result.label is expected
    if expected is EntailmentLabel.CONTRADICTS:
        assert result.verification_method == "lexical_founded"
        assert result.contradicting_passages
    else:
        assert result.contradicting_passages == ()


# Directional opposition needs a second model call confirming the same
# conditions and mutual exclusivity before it can withhold an idea.

_QUOTE = "Kinase X inhibition increased tumor growth threefold in AML cells."


def _install_replies(monkeypatch: pytest.MonkeyPatch, replies: list[Any]) -> list[Any]:
    requests: list[Any] = []

    async def completion(**kwargs: Any) -> Any:
        requests.append(kwargs)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return completion_response(json.dumps(reply))

    install_completion_backend(monkeypatch, completion)
    return requests


def _draft(quote: str = _QUOTE, passage: int = 1) -> dict[str, Any]:
    return _verdict("contradicts", contradicting=[_cite(passage, quote)])


def _confirmation(index: int = 1, **kwargs: Any) -> dict[str, Any]:
    return {
        "index": index,
        "same_conditions": True,
        "mutually_exclusive": True,
        **kwargs,
    }


def _assess_opposition(passage_id: str = "ev-1") -> ClaimAssessment:
    assessor, assessor_id = make_llm_assessor(_MODEL)
    return assess_claim(
        "Kinase X inhibition reduces tumor growth in AML cells.",
        [EvidencePassage(passage_id, _QUOTE)],
        assessor=assessor,
        assessor_id=assessor_id,
    )


@pytest.mark.parametrize("evidence_id", ["ev-1", "12345"])
def test_directional_opposition_is_verified_and_located(
    monkeypatch: pytest.MonkeyPatch, evidence_id: str
) -> None:
    requests = _install_replies(monkeypatch, [_draft(), {"verdicts": [_confirmation()]}])
    result = _assess_opposition(evidence_id)

    assert result.label is EntailmentLabel.CONTRADICTS
    assert result.verification_method == "model_opposition_verified"
    assert [s.evidence_id for s in result.contradicting_passages] == [evidence_id]
    assert result.contradicting_passages[0].quote == _QUOTE
    assert len(requests) == 2


def test_a_complete_negative_verification_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    negative = _confirmation(same_conditions=False, mutually_exclusive=False)
    requests = _install_replies(monkeypatch, [_draft(), {"verdicts": [negative]}])
    result = _assess_opposition()

    assert result.label is EntailmentLabel.INSUFFICIENT
    assert result.verification_method == "model_opposition_unconfirmed"
    assert len(requests) == 2


@pytest.mark.parametrize(
    "verdicts",
    [
        [],
        [_confirmation(same_conditions=False)],
        [_confirmation(), _confirmation()],
        [_confirmation(99)],
        [_confirmation(), {}],
    ],
)
def test_unconfirmed_opposition_remains_insufficient(
    monkeypatch: pytest.MonkeyPatch, verdicts: list[dict[str, Any]]
) -> None:
    _install_replies(
        monkeypatch,
        [_draft(), {"verdicts": verdicts}, {"verdicts": verdicts}],
    )
    result = _assess_opposition()

    assert result.label is EntailmentLabel.INSUFFICIENT
    assert result.contradicting_passages == ()
    assert result.verification_method == "model_opposition_unconfirmed"


@pytest.mark.parametrize(
    ("quote", "passage"),
    [
        ("Invented kinase tumor growth quote.", _QUOTE),
        (
            "Photosynthesis oxygenates the atmosphere.",
            f"{_QUOTE} Photosynthesis oxygenates the atmosphere.",
        ),
    ],
)
def test_unlocated_or_unrelated_quotes_never_request_verification(
    monkeypatch: pytest.MonkeyPatch, quote: str, passage: str
) -> None:
    requests = _install_replies(monkeypatch, [_draft(quote)])
    assessor, assessor_id = make_llm_assessor(_MODEL)
    result = assess_claim(
        "Kinase X inhibition reduces tumor growth in AML cells.",
        [EvidencePassage("ev-1", passage)],
        assessor=assessor,
        assessor_id=assessor_id,
    )

    assert result.label is EntailmentLabel.INSUFFICIENT
    assert result.verification_method == "contradiction_guard_rejected"
    assert len(requests) == 1


def test_unavailable_verification_is_observable_without_deterministic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_replies(
        monkeypatch,
        [_draft(), RuntimeError("unavailable"), RuntimeError("unavailable")],
    )
    with scoped_telemetry("test") as telemetry:
        result = _assess_opposition()

    rows = telemetry.snapshot().values()
    assert result.label is EntailmentLabel.INSUFFICIENT
    assert result.verification_method == "model_opposition_unconfirmed"
    assert sum(r["errors"].get("opposition_verification_unavailable", 0) for r in rows) == 1
    assert all(not r["deterministic_fallbacks"] for r in rows)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMCallBudgetExceededError(2, 1), LLMCallBudgetExceededError),
        (
            RateLimitError(
                message="free-models-per-day rate limit exceeded",
                llm_provider="openrouter",
                model="test-model",
            ),
            LLMRateLimitParkError,
        ),
    ],
    ids=["budget", "park"],
)
def test_verification_propagates_task_control_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: type[Exception]
) -> None:
    _install_replies(monkeypatch, [_draft(), error])
    with pytest.raises(expected):
        _assess_opposition()
