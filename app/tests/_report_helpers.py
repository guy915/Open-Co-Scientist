from typing import Any

from co_scientist.domains.report import markdown as report_markdown


def render_markdown(*, top_hypotheses: list[dict[str, Any]] | None = None, **fields: Any) -> str:
    hypotheses = (
        [
            {
                "id": "h1",
                "title": "NHE1 coupling",
                "statement": "NHE1 couples to the RSK axis in HFpEF.",
            }
        ]
        if top_hypotheses is None
        else top_hypotheses
    )
    fields.setdefault("research_goal", "Explain the cardiac benefit.")
    return report_markdown.render_report_markdown(
        report_markdown.ReportMarkdownInputs(provider="engine", top_hypotheses=hypotheses, **fields)
    )


def meta_review_markdown(meta_review: dict[str, object]) -> str:
    hypothesis: dict[str, object] = {
        "id": "h1",
        "title": "NHE1 coupling",
        "statement": "NHE1 couples to the RSK axis in HFpEF.",
    }
    return report_markdown.render_report_markdown(
        report_markdown.ReportMarkdownInputs(
            research_goal="Explain the cardiac benefit.",
            provider="engine",
            top_hypotheses=[hypothesis],
            meta_review=meta_review,
        )
    )
