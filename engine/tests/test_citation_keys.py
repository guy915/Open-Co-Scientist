from co_scientist.core.citations import citation_keys_in, strip_citation_markers
from co_scientist.domains.research_state.drain.reviews import _claim_cited_by
from co_scientist.science.citations import resolve_citation_keys


def test_grouped_and_adjacent_keys_resolve_once_in_appearance_order() -> None:
    text = "First [C2, C1][C3]. Repeated [C2], unknown [C99], prose [control group]."
    sources = {key: {"title": key} for key in ("C1", "C2", "C3")}
    assert citation_keys_in(text) == ["C2", "C1", "C3", "C2", "C99"]
    assert list(resolve_citation_keys(text, sources)) == ["C2", "C1", "C3"]


def test_sentence_attribution_uses_the_same_groups_for_existing_source_namespaces() -> None:
    text = "A supported claim [C1, attachment-a]. Unrelated claim [C2]."
    assert _claim_cited_by(text, "C1") == "A supported claim [C1, attachment-a]."
    assert _claim_cited_by(text, "attachment-a") == "A supported claim [C1, attachment-a]."
    assert _claim_cited_by(text, "C2") == "Unrelated claim [C2]."


def test_source_marker_stripping_keeps_generated_keys_and_ordinary_brackets() -> None:
    text = "Result [1, 2] (Smith 2024), linked [C1, C2] and [control group]."
    assert strip_citation_markers(text) == "Result  , linked [C1, C2] and [control group]."
