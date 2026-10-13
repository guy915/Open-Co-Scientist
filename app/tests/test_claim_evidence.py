from __future__ import annotations

from collections.abc import Sequence

import pytest
from co_scientist.domains.research_state.claims import (
    ClaimAssessment,
    EntailmentLabel,
    EvidencePassage,
    GateDecision,
    _locate_all,
    as_passages,
    assess_claim,
    assess_claims_batch,
    publication_gate,
)

# Verbatim production pairs exposed whole-passage negation paired with unrelated
# or confirming quotes.

# Greek letters are verbatim source data and must not be transliterated.
# ruff: noqa: RUF001

# Negation in abstract boilerplate must not contradict an unrelated cited
# sentence.
_UNRELATED_NEGATION = "Body weight did not differ between groups."


def _passage(*sentences: str) -> EvidencePassage:
    return EvidencePassage(
        evidence_id="e1",
        text=" ".join(sentences),
        source="pubmed",
        url="https://example.org/1",
    )


def _quotes(spans: Sequence[object]) -> list[str]:
    return [getattr(span, "quote", "") for span in spans]


_REDUCTION_CLAIM = (
    "Together, these actions reduce collagen synthesis and α-SMA "
    "expression, attenuating the fibrotic phenotype."
)
_REDUCTION_QUOTE = (
    "Treatment with LGK-974 significantly reduced both α-SMA and "
    "collagen type I expression, whereas ETC-159 selectively decreased "
    "collagen type I."
)
_METFORMIN_CLAIM = (
    "Metformin activates AMPK in human cardiac fibroblasts, leading to "
    "inhibitory phosphorylation of Smad3 (Ser203/207) and suppression of "
    "HIF-1α-mediated glycolysis, thereby blocking TGF-β-induced "
    "myofibroblast transition and collagen deposition."
)
_PIRFENIDONE_QUOTE = (
    "Furthermore, pirfenidone attenuated TGF-β1- and CTHRC1-induced "
    "fibroblast activity, upregulation of bone morphogenic protein-4"
    "(BMP-4)/Gremlin1, and downregulation of α-smooth muscle actin, "
    "fibronectin, and FHL2, similar to that observed post-CTHRC1 inhibition."
)
_AMPK_CLAIM = "Metformin enters cardiac fibroblasts and activates AMPK via LKB1."
_GLUCOSE_QUOTE = (
    "We found that glucose starvation transiently activates AMPK, whereas "
    "changes in glucagon and insulin levels had no impact on AMPK."
)
_COLLAGEN_CLAIM = "Metformin reduces collagen I expression in human cardiac fibroblasts."
_ON_SUBJECT_NEGATION = (
    "Metformin did not reduce collagen I expression in human cardiac fibroblasts."
)


@pytest.mark.parametrize(
    ("claim", "sentences", "quote"),
    [
        (
            _REDUCTION_CLAIM,
            [
                "Wnt inhibition and the fibrotic phenotype in cardiac fibroblasts.",
                _REDUCTION_QUOTE,
            ],
            _REDUCTION_QUOTE,
        ),
        (
            _METFORMIN_CLAIM,
            [
                "Pirfenidone attenuates lung fibrotic fibroblast responses "
                "to transforming growth factor-β1.",
                _PIRFENIDONE_QUOTE,
                "Myofibroblast transition and collagen deposition were "
                "assessed in human cardiac fibroblasts after TGF-β1 "
                "stimulation.",
                "AMPK phosphorylation, Smad3 phosphorylation and glycolysis "
                "were measured; HIF-1α protein was quantified.",
            ],
            _PIRFENIDONE_QUOTE,
        ),
        (
            _AMPK_CLAIM,
            [
                "AMPK activation in fibroblasts under nutrient stress.",
                _GLUCOSE_QUOTE,
            ],
            _GLUCOSE_QUOTE,
        ),
    ],
    ids=["confirmatory", "off-drug", "off-stimulus"],
)
def test_a_negation_about_something_else_is_not_a_contradiction(
    claim: str, sentences: list[str], quote: str
) -> None:
    passage = _passage(*sentences, _UNRELATED_NEGATION)

    result = assess_claim(claim, [passage])

    assert result.label is not EntailmentLabel.CONTRADICTS
    assert quote not in _quotes(result.contradicting_passages)


def test_on_subject_negation_contradicts_and_is_the_cited_quote() -> None:
    passage = _passage(
        "Metformin reduces collagen I expression in human cardiac fibroblasts, we hypothesized.",
        "Cells were treated for 48 hours.",
        _ON_SUBJECT_NEGATION,
    )

    result = assess_claim(_COLLAGEN_CLAIM, [passage])

    assert result.label is EntailmentLabel.CONTRADICTS
    assert _quotes(result.contradicting_passages) == [_ON_SUBJECT_NEGATION]


def test_batch_fallback_cannot_yield_an_unfounded_contradiction() -> None:
    passage = _passage(
        "Wnt inhibition and the fibrotic phenotype in cardiac fibroblasts.",
        _REDUCTION_QUOTE,
        _UNRELATED_NEGATION,
    )
    (result,) = assess_claims_batch(
        [_REDUCTION_CLAIM],
        [passage],
        batch_assessor=lambda claims, _: [None] * len(claims),
        assessor_id="llm:m",
    )

    assert result.assessor == "llm:m"
    assert result.label is not EntailmentLabel.CONTRADICTS
    assert _REDUCTION_QUOTE not in _quotes(result.contradicting_passages)


_CONTRADICTED_CLAIM = "Kinase X inhibition reduces AML tumor growth."
_UNRELATED = as_passages(["An unrelated passage about photosynthesis."])


def _contradicted_assessments() -> list[ClaimAssessment]:
    return [
        assess_claim(
            _CONTRADICTED_CLAIM,
            as_passages(["Kinase X inhibition did not reduce AML tumor growth."]),
        )
    ]


def test_gate_blocks_a_categorical_contradiction_whatever_is_allowed() -> None:
    assessments = _contradicted_assessments()

    assert publication_gate(assessments).decision is GateDecision.BLOCK
    loosened = publication_gate(assessments, allow_speculative=True, require_supported_claim=False)
    assert loosened.decision is GateDecision.BLOCK


def test_gate_allows_a_contradicted_claim_the_idea_only_proposes() -> None:
    result = publication_gate(
        _contradicted_assessments(),
        explicitly_speculative_claims={_CONTRADICTED_CLAIM},
    )

    assert result.decision is GateDecision.ALLOW
    assert result.contradicted_claims == (_CONTRADICTED_CLAIM,)
    assert "contradicted" in result.reason


def test_gate_blocks_a_categorical_contradiction_beside_a_speculative_one() -> None:
    grounding = "Kinase X inhibition reduces AML relapse rates."
    assessments = [
        *_contradicted_assessments(),
        assess_claim(
            grounding,
            as_passages(
                [
                    "Kinase X inhibition did not reduce AML relapse rates; "
                    "there was no significant effect on relapse."
                ]
            ),
        ),
    ]

    result = publication_gate(assessments, explicitly_speculative_claims={_CONTRADICTED_CLAIM})

    assert result.decision is GateDecision.BLOCK
    assert result.failed_claims == (grounding,)


def test_gate_withholds_unsupported_claims_unless_explicitly_speculative() -> None:
    proposed = assess_claim("We hypothesize kinase X may alter neuronal recovery.", _UNRELATED)
    categorical = assess_claim(
        "Kinase X is established as the neuronal recovery controller.",
        _UNRELATED,
    )
    speculative = {proposed.claim}

    assert publication_gate([proposed]).decision is GateDecision.BLOCK
    blocked = publication_gate([proposed, categorical], explicitly_speculative_claims=speculative)
    assert blocked.decision is GateDecision.BLOCK

    allowed = publication_gate([proposed], explicitly_speculative_claims=speculative)
    assert allowed.decision is GateDecision.ALLOW

    ungrounded = publication_gate(
        [proposed],
        explicitly_speculative_claims=speculative,
        require_supported_claim=True,
    )
    assert ungrounded.decision is GateDecision.BLOCK
    assert ungrounded.reason == "no evidence-supported contextual claim"


@pytest.mark.parametrize(
    ("claim", "passage", "label"),
    [
        (
            "Kinase X inhibition reduces AML tumor growth.",
            "Kinase X inhibition reduces AML tumor growth in cell lines.",
            EntailmentLabel.SUPPORTS,
        ),
        (
            "Inhibiting kinase X reduces tumor growth in AML cells.",
            "Kinase enzymes regulate cellular growth under diverse "
            "metabolic conditions across many organisms.",
            EntailmentLabel.PARTIAL,
        ),
    ],
)
def test_gate_allows_supported_and_partially_supported_claims(
    claim: str, passage: str, label: EntailmentLabel
) -> None:
    assessment = assess_claim(claim, as_passages([passage]))

    assert assessment.label is label
    result = publication_gate([assessment], require_supported_claim=True)
    assert result.decision is GateDecision.ALLOW


def test_gate_blocks_hypothesis_with_no_claims() -> None:
    assert publication_gate([]).decision is GateDecision.BLOCK


_PASSAGES = (
    EvidencePassage(
        evidence_id="7f1c9a20-1b2e-4f3a-9c8d-0a1b2c3d4e5f#0",
        text=(
            "Fasudil could down-regulate RhoA, GEF-H1, and phosphorylated "
            "GEF-H1 to inhibit ROCK signaling and further reduce Col I "
            "expression and the myofibroblast proportion."
        ),
        source="pubmed",
        url="https://example.org/fasudil",
    ),
    EvidencePassage(
        evidence_id="2b8e4d11-5c6f-4a7b-8d9e-1f2a3b4c5d6e#1",
        text=(
            "Verteporfin has been shown to effectively inhibit YAP-TEAD "
            "function in vivo in cardiac fibroblasts."
        ),
        source="web",
        url="https://example.org/verteporfin",
    ),
)
_FASUDIL_ID = _PASSAGES[0].evidence_id
_FASUDIL_PARENT = _FASUDIL_ID.split("#")[0]
_LATER = EvidencePassage(
    evidence_id=f"{_FASUDIL_PARENT}#1",
    text="A later chunk describes a separate measured endpoint.",
)
_NUMBERED = (
    EvidencePassage("other", "The first passage has this claim quote."),
    EvidencePassage("1", "The second passage has this claim quote."),
)
_CHUNKED = (
    EvidencePassage("12345#0", "First article chunk."),
    EvidencePassage("12345#1", "Later article chunk has the claim quote."),
)


@pytest.mark.parametrize(
    ("citation", "passages", "cites_ids", "resolved"),
    [
        (("1", "reduce Col I expression"), _PASSAGES, False, [_FASUDIL_ID]),
        (("1", "claim quote"), _NUMBERED, False, ["other"]),
        (("1", "claim quote"), _NUMBERED, True, ["1"]),
        (
            ("99", "another article"),
            (EvidencePassage("99", "another article"),),
            False,
            [],
        ),
        (("12345", "claim quote"), _CHUNKED, True, ["12345#1"]),
        (
            (_FASUDIL_PARENT, "inhibit ROCK signaling"),
            _PASSAGES,
            True,
            [_FASUDIL_ID],
        ),
        (
            (_FASUDIL_PARENT, "separate measured endpoint"),
            (*_PASSAGES, _LATER),
            True,
            [_LATER.evidence_id],
        ),
        (
            (_FASUDIL_ID, "inhibit YAP-TEAD function in vivo"),
            _PASSAGES,
            True,
            [],
        ),
        (
            ("unrecognized-legacy-id", "inhibit YAP-TEAD function"),
            _PASSAGES,
            True,
            [],
        ),
        ((_FASUDIL_ID, "olaparib cures cardiac fibrosis"), _PASSAGES, True, []),
    ],
    ids=[
        "passage-number",
        "numeric-id-cannot-shadow-a-passage-number",
        "numeric-id-when-the-assessor-cites-ids",
        "out-of-range-number-is-not-an-id",
        "parent-id-reaches-a-later-chunk",
        "id-without-the-chunk-suffix",
        "parent-id-quote-in-a-later-chunk",
        "verbatim-quote-under-another-passages-id",
        "verbatim-quote-under-an-unknown-id",
        "quote-in-no-shown-passage",
    ],
)
def test_a_citation_resolves_only_to_a_passage_that_contains_the_quote(
    citation: tuple[str, str],
    passages: tuple[EvidencePassage, ...],
    cites_ids: bool,
    resolved: list[str],
) -> None:
    spans = _locate_all((citation,), passages, cites_evidence_ids=cites_ids)

    assert [s.evidence_id for s in spans] == resolved
