from co_scientist.domains.chat.goal_text import clean_title
from co_scientist.domains.chat.interviews import model
from co_scientist.domains.chat.titles import title_case


def test_title_case_capitalizes_plain_words_and_keeps_scientific_spellings() -> None:
    assert title_case("hello") == "Hello"
    assert (
        title_case("role of mRNA decay in the FDA-approved p53 pathway")
        == "Role of mRNA Decay in the FDA-approved p53 Pathway"
    )
    assert title_case("β-catenin signalling in glioma") == "β-catenin Signalling in Glioma"
    assert title_case("what to look for") == "What to Look For"
    assert title_case("") == ""


def test_generated_run_titles_are_title_case() -> None:
    assert clean_title(' "ferroptosis escape in glioma stem cells." ') == (
        "Ferroptosis Escape in Glioma Stem Cells"
    )
    assert clean_title("   ") is None


def test_interview_titles_are_asked_for_and_stored_in_title_case() -> None:
    assert "Title: an optional concise title in Title Case." in model._SYSTEM_PROMPT
    fields = model._normalized_fields({"title": "  senolytic clearance in aged tissue "})
    assert fields["title"] == "Senolytic Clearance in Aged Tissue"
    assert model._normalized_fields({"title": None})["title"] is None
