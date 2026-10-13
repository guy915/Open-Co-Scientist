from co_scientist.domains.report.markdown.overview import research_overview_sections

from tests._report_helpers import meta_review_markdown as _meta_review_markdown
from tests._report_helpers import render_markdown


def _overview_markdown(payload: dict[str, object]) -> str:
    return "\n".join(line for section in research_overview_sections(payload) for line in section)


def test_main_research_directions_sit_immediately_before_top_hypotheses() -> None:
    markdown = render_markdown(
        meta_review={
            "main_research_directions": (
                "One direction is **Metabolic State**, which matters.\n\n"
                "A second is **MazEF-State Biomarking**. Unexpectedly, both "
                "may track one program."
            )
        }
    )

    assert "One direction is **Metabolic State**, which matters." in markdown
    assert "A second is **MazEF-State Biomarking**." in markdown
    assert markdown.index("## Main Research Directions") < markdown.index("## Top hypotheses")


def test_themes_render_with_sub_themes_and_older_flat_shapes() -> None:
    markdown = _meta_review_markdown(
        {
            "recurring_themes": [
                {
                    "theme": "Core Hypothesis",
                    "description": "How the mechanism is argued.",
                    "frequency": "very common",
                    "sub_themes": [
                        {
                            "theme": "Driver vs. Consequence",
                            "description": "Whether it initiates or follows.",
                            "points": ["Show the temporal sequence."],
                        },
                        {
                            "theme": "Incremental vs. Groundbreaking",
                            "description": "Builds on existing knowledge.",
                            "points": [],
                        },
                        "State every assumption explicitly.",
                    ],
                },
                {
                    "theme": "Motor neuron specificity",
                    "description": "Rarely explained.",
                    "frequency": "common",
                },
                {"theme": "", "description": "orphaned text", "frequency": ""},
            ],
        }
    )
    bare = _meta_review_markdown({"emerging_themes": ["Time-resolved state measurements"]})

    assert "### Emerging themes" in markdown
    assert "#### Core Hypothesis" in markdown
    assert "- **Driver vs. Consequence**: Whether it initiates" in markdown
    assert "  - Show the temporal sequence." in markdown
    assert "- **Incremental vs. Groundbreaking**: Builds on" in markdown
    assert "- State every assumption explicitly." in markdown
    assert "#### Motor neuron specificity" in markdown
    assert "Rarely explained." in markdown
    assert "- Time-resolved state measurements" in bare
    assert "orphaned text" not in markdown
    assert "****:" not in markdown


def test_recommendations_render_as_a_primary_step_and_a_numbered_roadmap() -> None:
    markdown = _meta_review_markdown(
        {
            "strategic_recommendations": [
                {
                    "focus_area": "Calcium handling",
                    "recommendation": "Measure MCU flux directly.",
                    "justification": "The current data is indirect.",
                    "time_estimate": "Weeks 1-2",
                    "recommended_idea": "Hypothesis 1, building on 4",
                },
                {
                    "focus_area": "Validation",
                    "recommendation": "Confirm the mechanism in vivo.",
                    "phase_label": "Phase A",
                },
                "A bare-string recommendation.",
            ]
        }
    )

    assert "### Recommendation and strategic roadmap" in markdown
    assert "### Strategic recommendations" not in markdown
    assert (
        "**Primary recommendation:** **Calcium handling**: Measure MCU"
        " flux directly. (Weeks 1-2)" in markdown
    )
    assert "The current data is indirect." in markdown
    assert "Recommended idea: Hypothesis 1, building on 4" in markdown
    assert "1. Phase A: **Validation**: Confirm the mechanism in vivo." in markdown
    assert "2. A bare-string recommendation." in markdown
