from __future__ import annotations

from collections.abc import Sequence

import pytest
from co_scientist.domains.research_state.claims import (
    AssessorDraft,
    EntailmentLabel,
    EvidencePassage,
    as_passages,
    assess_claim,
    extract_atomic_claims,
)
from co_scientist.domains.research_state.claims.chunking import (
    chunk_evidence_passage,
    parent_evidence_id,
)


@pytest.mark.parametrize(
    ("claim", "passage"),
    [
        ("p53 inhibits cancer invasion.", "p53 curbs cellular migration."),
        (
            "DNA contains hereditary information.",
            "DNA encodes inherited instructions.",
        ),
        (
            "Protein H folds cooperatively.",
            "H adopts native structure through a concerted transition.",
        ),
        ("Locus J/K predicts trait Z.", "J and K cosegregate with Z."),
    ],
)
def test_short_identifier_passage_reaches_semantic_assessment(claim: str, passage: str) -> None:
    seen: list[EvidencePassage] = []

    def assess(_claim: str, passages: Sequence[EvidencePassage]) -> AssessorDraft:
        seen.extend(passages)
        return AssessorDraft(EntailmentLabel.INSUFFICIENT)

    result = assess_claim(claim, as_passages([passage]), assessor=assess)

    assert [p.text for p in seen] == [passage]
    assert result.label is EntailmentLabel.INSUFFICIENT


def test_extracts_atomic_claims_and_drops_fragments() -> None:
    text = (
        "Inhibiting kinase X reduces tumor growth in AML cells. "
        "Ok. "
        "The mechanism involves downstream apoptosis signaling. "
        "Inhibiting kinase X reduces tumor growth in AML cells."
    )

    assert extract_atomic_claims(text) == [
        "Inhibiting kinase X reduces tumor growth in AML cells.",
        "The mechanism involves downstream apoptosis signaling.",
    ]


@pytest.mark.parametrize(
    ("gaps", "kept"),
    [
        (
            "Within the retrieved literature, no source tests whether PI3K "
            "inhibition alone reactivates the composite program. "
            "This interaction appears unexplored in the retrieved literature. "
            "This hypothesis is formulated without access to a literature "
            "review; no citation keys are available. "
            "The apoptotic mechanism is not systematically characterized. "
            "Dual blockade has not been tested in this subtype.",
            "Menin inhibition destabilizes c-Myc in KMT2A-rearranged AML.",
        ),
        (
            "We did not find any source in the provided literature directly "
            "testing CDK4/6 inhibitors in human cardiac fibroblasts. "
            "Direct testing of niclosamide in primary HCF under Wnt-active "
            "conditions appears unreported. "
            "The meta-review notes under-explored cytoskeletal control "
            "mechanisms.",
            "Fasudil inhibits ROCK1 and ROCK2 in cardiac fibroblasts.",
        ),
    ],
    ids=["synthetic-gaps", "production-wordings"],
)
def test_extraction_drops_sentences_asserting_an_evidence_gap(gaps: str, kept: str) -> None:
    # Corpus-absence statements are negative existentials no retrieved passage
    # can confirm.
    assert extract_atomic_claims(f"{kept} {gaps}") == [kept]


# Long passages expose union-denominator caps hidden by one-sentence fixtures.
_SUPPORTING_ABSTRACT = (
    "Selective kinase X blockade in acute myeloid leukemia: preclinical "
    "evidence across patient-derived models. "
    "Acute myeloid leukemia remains difficult to treat, and the contribution "
    "of kinase X to disease maintenance has not been established in primary "
    "material. We profiled expression across sixty-one primary specimens and "
    "eleven established lines, then applied a selective small-molecule "
    "antagonist alongside cytarabine and an isotype-matched vehicle control. "
    "Target engagement was verified by phosphoproteomic readout at three "
    "separate residues. Blocking the kinase curtailed tumor "
    "proliferation in every AML model tested, with cell-cycle arrest at the "
    "G1 checkpoint and induction of apoptosis in treated cells within "
    "forty-eight hours. Colony formation from healthy donor progenitors was "
    "unaffected at equivalent concentrations, suggesting a usable "
    "therapeutic window. Transcriptional profiling implicated downstream "
    "signaling through the canonical survival axis rather than off-target "
    "activity. These results support further evaluation of this strategy in "
    "acute myeloid leukemia."
)
_KINASE_CLAIM = "Inhibiting kinase X reduces tumor growth in AML cells."
_MIDBAND = (
    "Kinase enzymes regulate cellular growth under diverse metabolic "
    "conditions across many organisms."
)


def _passage(text: str) -> EvidencePassage:
    return EvidencePassage("ev-1", text, source="pubmed", url="https://x.org/1")


@pytest.mark.parametrize(
    ("passages", "label", "quote"),
    [
        (
            [_passage("This unrelated review discusses cardiac tissue growth.")],
            EntailmentLabel.INSUFFICIENT,
            None,
        ),
        (
            [_passage(_SUPPORTING_ABSTRACT)],
            EntailmentLabel.SUPPORTS,
            "curtailed tumor proliferation",
        ),
        (
            # Literal containment must reach SUPPORTS even in a long passage.
            [
                _passage(
                    _SUPPORTING_ABSTRACT.replace(
                        "These results support",
                        f"{_KINASE_CLAIM} These results support",
                    )
                )
            ],
            EntailmentLabel.SUPPORTS,
            _KINASE_CLAIM,
        ),
        (
            [_passage(_MIDBAND)],
            EntailmentLabel.PARTIAL,
            "kinase enzymes regulate",
        ),
        (
            [
                _passage(_MIDBAND),
                EvidencePassage(
                    "ev-2",
                    "Kinase X inhibition reduces tumor growth across several AML cells.",
                ),
            ],
            EntailmentLabel.SUPPORTS,
            "kinase x inhibition reduces",
        ),
        (
            [
                _passage("Kinase X inhibition reduces tumor growth in AML cells."),
                _passage("Kinase X inhibition did not reduce tumor growth in AML cells."),
            ],
            EntailmentLabel.CONTRADICTS,
            None,
        ),
    ],
    ids=[
        "weak-overlap",
        "strong-topical-overlap",
        "claim-quoted-verbatim",
        "mid-band-overlap",
        "full-support-beats-a-partial-near-miss",
        "contradiction-dominates-support",
    ],
)
def test_the_deterministic_assessor_labels_by_overlap_and_locates_support(
    passages: list[EvidencePassage], label: EntailmentLabel, quote: str | None
) -> None:
    result = assess_claim(_KINASE_CLAIM, passages)

    assert result.label is label
    assert result.assessor
    if label is EntailmentLabel.CONTRADICTS:
        assert result.contradicting_passages
    if label is EntailmentLabel.INSUFFICIENT:
        assert result.supporting_passages == ()
    if quote:
        span = result.supporting_passages[0]
        by_id = {p.evidence_id: p for p in passages}
        assert by_id[span.evidence_id].text[span.start : span.end] == span.quote
        assert quote in span.quote.lower() or span.quote == quote


@pytest.mark.parametrize("label", [EntailmentLabel.SUPPORTS, EntailmentLabel.PARTIAL])
def test_a_support_verdict_without_a_locatable_quote_is_downgraded(
    label: EntailmentLabel,
) -> None:
    def fabricating(claim: str, passages: list[EvidencePassage]) -> AssessorDraft:
        return AssessorDraft(label=label, supporting=(("ev-1", "a quote that is nowhere"),))

    result = assess_claim(
        "Kinase X inhibition reduces tumor growth.",
        [EvidencePassage("ev-1", "Unrelated passage text.")],
        assessor=fabricating,  # type: ignore[arg-type]
        assessor_id="llm:test",
    )

    assert result.label is EntailmentLabel.INSUFFICIENT
    assert result.supporting_passages == ()
    assert result.assessor == "llm:test"


def test_deep_supporting_sentence_in_a_chunked_article_is_located() -> None:
    # Chunk offsets must map deep article quotes back to their parent evidence
    # record.
    filler = "Unrelated background discussion sentence about other topics. "
    needle = "Kinase X inhibition reduces tumor growth in AML cell lines."
    chunks = chunk_evidence_passage(
        "article-99",
        head_text="Title only, no abstract.",
        body_text=(filler * 300) + needle + (" " + filler * 300),
        source="pubmed",
        url="https://example.org/99",
    )
    assert len(chunks) > 1

    result = assess_claim(needle, chunks, top_k=3)

    assert result.label is EntailmentLabel.SUPPORTS
    span = result.supporting_passages[0]
    assert needle in span.quote or span.quote in needle
    assert parent_evidence_id(span.evidence_id) == "article-99"
