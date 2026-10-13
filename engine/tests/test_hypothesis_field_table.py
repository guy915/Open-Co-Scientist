from co_scientist.domains.chat.qa.snapshot import idea_view
from co_scientist.domains.research_state.hypothesis_fields import store_text_fields
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.safety.hypothesis import hypothesis_text
from co_scientist.domains.safety.hypothesis.safety import redact_fields
from co_scientist.domains.safety.hypothesis_text import screened_text
from co_scientist.domains.safety.rules import REDACTED_PLACEHOLDER, redact_hypothesis_fields
from co_scientist.science.safety_screen import _screen_text


def test_engine_and_store_screen_all_four_fields_in_their_existing_order() -> None:
    engine = {
        "text": "Claim",
        "explanation": "Effect",
        "literature_grounding": "Mechanism",
        "experiment": "Experiment",
    }
    stored = store_text_fields(engine)
    assert stored == {
        "statement": "Claim",
        "mechanism": "Mechanism",
        "expected_effect": "Effect",
        "experimental_context": "Experiment",
    }
    assert screened_text(engine, "engine", separator=" ") == "Claim Effect Mechanism Experiment"
    assert _screen_text(Hypothesis.from_dict(engine)) == "Claim Effect Mechanism Experiment"
    assert hypothesis_text(stored) == "Claim\nMechanism\nEffect\nExperiment"
    assert {name: idea_view(engine)[name] for name in stored} == stored


def test_redaction_keeps_the_statement_and_hides_every_screened_detail_in_both_namespaces() -> None:
    engine = {
        "text": "Claim",
        "explanation": "Effect",
        "literature_grounding": "Mechanism",
        "experiment": "Experiment",
    }
    stored = store_text_fields(engine)
    redacted = redact_fields(engine)
    assert redacted["text"] == "Claim"
    assert all(redacted[name] == REDACTED_PLACEHOLDER for name in engine if name != "text")
    redacted_store = redact_fields(stored)
    assert redacted_store["statement"] == "Claim"
    assert all(
        redacted_store[name] == REDACTED_PLACEHOLDER for name in stored if name != "statement"
    )
    assert redact_hypothesis_fields("Claim", "Effect", "Experiment", "Mechanism") == (
        "Claim",
        REDACTED_PLACEHOLDER,
        REDACTED_PLACEHOLDER,
        REDACTED_PLACEHOLDER,
    )


def test_missing_detail_stays_absent_and_unrelated_fields_are_untouched() -> None:
    assert redact_hypothesis_fields("Claim", None, "", None) == ("Claim", None, "", None)
    assert redact_fields({"title": "Title", "text": "Claim"}) == {"title": "Title", "text": "Claim"}
