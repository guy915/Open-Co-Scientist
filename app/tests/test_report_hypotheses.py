import pytest
from co_scientist.domains.report.markdown import hypothesis as report_markdown_hypothesis
from co_scientist.domains.report.markdown.hypothesis import (
    _HYPOTHESIS_DISCLAIMER,
)

from tests._report_helpers import meta_review_markdown as _meta_review_markdown
from tests._report_helpers import render_markdown

# Contact examples can reference the full synthesis pool, beyond the five-item
# report slice.


def test_comparison_a_populated_comparison_renders_domain_aware_axes() -> None:
    markdown = _meta_review_markdown(
        {
            "candidate_comparison": {
                "thematic_summary": ("The ideas split into two mechanistic themes."),
                "axes": ["Off-target risk", "Model system"],
                "ideas": [
                    {
                        "idea": "Hypothesis 1: NHE1 blockade",
                        "values": [
                            "Low -- selective for cardiac NHE1.",
                            "Isogenic organoid pairs.",
                        ],
                    }
                ],
            }
        }
    )

    assert "### Comparison of candidate ideas" in markdown
    assert "The ideas split into two mechanistic themes." in markdown
    assert "Hypothesis 1: NHE1 blockade" in markdown
    assert "**Off-target risk:** Low -- selective for cardiac NHE1." in markdown
    assert "**Model system:** Isogenic organoid pairs." in markdown
    assert "Computational scalability" not in markdown


# The assembled snapshot catches ordering/separator drift that subsection-only
# tests miss.


_NOTICE_PREFIX = "**Scientist-contributed — not yet reviewed:**"


def test_unreviewed_scientist_admission_notice_leads_the_entry() -> None:
    hyp = {
        "id": "h5",
        "title": "Scientist idea.",
        "elo_rating": 1300,
        "created_by_agent": "scientist_manual",
        "statement": "A contributed statement.",
    }

    lines = report_markdown_hypothesis._render_hypothesis_entry(1, hyp, [], [], [])

    assert lines[1] == _HYPOTHESIS_DISCLAIMER
    assert lines[2] == ""
    assert lines[3].startswith(_NOTICE_PREFIX)
    assert lines[4] == ""
    assert "**Proposed hypothesis:** A contributed statement." in lines


# Absent corpus verification must be disclosed; model novelty judgments are not
# settled facts.


def _hypothesis(identifier: str, title: str, **extra: object) -> dict[str, object]:
    return {
        "id": identifier,
        "title": title,
        "statement": f"{title} changes the measured phenotype.",
        **extra,
    }


@pytest.mark.parametrize(
    ("extra", "disclosed"),
    [
        ({}, True),
        ({"novelty_validation": "Checked against 4 retrieved papers."}, False),
    ],
)
def test_novelty_is_disclosed_as_unverified_unless_checked_against_literature(
    extra: dict[str, object], disclosed: bool
) -> None:
    markdown = render_markdown(
        research_goal="Map the feedback loop.",
        top_hypotheses=[_hypothesis("h1", "Feedback control", **extra)],
    )

    assert ("reviewing model's own judgment" in markdown) is disclosed


def test_persisted_sentinel_dois_do_not_merge_distinct_papers() -> None:
    """Runs recorded before the parser fix hold "<not found>" in the doi
    column, which is one truthy value shared by every unidentified paper.
    """
    rows = [
        {"id": "ev-1", "title": "First paper", "doi": "<not found>"},
        {"id": "ev-2", "title": "Second paper", "doi": "<not found>"},
        {"id": "ev-3", "title": "Third paper", "doi": "10.1000/real"},
    ]

    deduped = report_markdown_hypothesis._dedupe_evidence(rows)

    assert [row["id"] for row in deduped] == ["ev-1", "ev-2", "ev-3"]
    assert report_markdown_hypothesis._identifier_suffix(rows[0]) == ""
