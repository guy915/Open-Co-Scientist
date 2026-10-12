from __future__ import annotations

from co_scientist.domains.report import markdown as report_markdown

from evaluations._paired_db import _report_ideas


def test_the_paired_reader_sorts_engine_report_entries_into_featured_and_screened() -> None:
    finalist = {"id": "f1", "title": "Finalist idea", "statement": "Lactate shuttling."}
    screened = {"id": "s1", "title": "Screened idea", "statement": "Glycogen release."}
    markdown = report_markdown.render_report_markdown(
        report_markdown.ReportMarkdownInputs(
            research_goal="Explain ATP recovery.",
            provider="engine",
            top_hypotheses=[finalist],
            screened_hypotheses=[screened],
        )
    )

    rows = [{"id": "f1", "title": "Finalist idea"}, {"id": "s1", "title": "Screened idea"}]

    assert _report_ideas(markdown, rows) == {"featured": ["f1"], "screened": ["s1"]}  # type: ignore[arg-type]
