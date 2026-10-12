from co_scientist.core.text_matching import coverage, tokenize
from co_scientist.domains.research_state.claims.assessor import _lexical_score
from co_scientist.domains.research_state.text_utils import token_coverage
from co_scientist.platform.retrieval.citations import _token_overlap
from co_scientist.platform.retrieval.run_corpus import _tokenize


def test_literal_claim_containment_is_full_coverage_across_matchers() -> None:
    claim = "TP53 loss impairs apoptosis in colorectal tumour cells"
    passage = "TP53, loss; impairs apoptosis, in colorectal tumour cells."
    assert _token_overlap(claim, passage) == 1.0
    assert _lexical_score(claim, passage) == 1.0
    assert token_coverage(claim, passage) == 1.0


def test_tokenization_keeps_term_frequency_and_caller_specific_filtering() -> None:
    assert tokenize("AML, TP53; AML.") == ("aml", "tp53", "aml")
    assert tokenize("AML and TP53", min_len=4, stopwords={"tp53"}) == ()
    assert _tokenize("AML, TP53; AML.") == ["aml", "tp53", "aml"]
    assert _lexical_score("AML", "acute myeloid leukemia") == 1.0


def test_coverage_remains_directional_and_does_not_treat_absence_as_support() -> None:
    assert coverage(frozenset({"tp53"}), frozenset({"tp53", "cells"})) == 1.0
    assert coverage(frozenset({"tp53", "cells"}), frozenset({"tp53"})) == 0.5
    assert coverage(frozenset(), frozenset({"tp53"})) == 0.0
