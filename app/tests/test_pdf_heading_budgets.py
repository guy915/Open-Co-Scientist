from __future__ import annotations

from types import SimpleNamespace

import pytest
from co_scientist.domains.documents import pdf
from co_scientist.domains.documents.ingest import _apply_heading_markup


def test_outline_budget_reaches_ingestion_without_fallback() -> None:
    reader = SimpleNamespace(
        outline=[SimpleNamespace(title="Results")] * (pdf.MAX_OUTLINE_ENTRIES + 1)
    )
    reader.get_destination_page_number = lambda _: 0
    with pytest.raises(pdf.HeadingBudgetExceededError):
        _apply_heading_markup(reader, [], ["Results"])


def test_deep_outline_is_rejected_before_recursion_limit() -> None:
    outline: list[object] = [SimpleNamespace(title="Results")]
    for _ in range(pdf.MAX_OUTLINE_DEPTH + 1):
        outline = [outline]
    with pytest.raises(pdf.HeadingBudgetExceededError, match="depth"):
        pdf.flatten_outline(SimpleNamespace(outline=outline))


def test_fuzzy_comparison_budget_is_shared_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    budget = pdf._HeadingBudget(1)
    assert pdf.raw_bookmark_matches([("abc", 1)], ["xyz"], budget) == {}
    with pytest.raises(pdf.HeadingBudgetExceededError, match="comparison"):
        pdf.raw_bookmark_matches([("abc", 1)], ["xyz"], budget)


def test_exact_bookmarks_need_no_fuzzy_work_at_the_page_limit() -> None:
    lines = [f"Heading {i}" for i in range(pdf.MAX_PAGE_BOOKMARKS)]
    outline = [(line, 1) for line in lines]
    assert len(pdf.raw_bookmark_matches(outline, lines, pdf._HeadingBudget(0))) == len(lines)


@pytest.mark.parametrize("kind", ["bookmarks", "lines", "title"])
def test_heading_inputs_are_bounded_before_fuzzy_work(kind: str) -> None:
    outline = [("Results", 1)]
    lines = ["Results"]
    if kind == "bookmarks":
        outline *= pdf.MAX_PAGE_BOOKMARKS + 1
    elif kind == "lines":
        lines *= pdf.MAX_PAGE_LINES + 1
    else:
        outline = [("x" * (pdf.MAX_HEADING_CHARS + 1), 1)]
    with pytest.raises(pdf.HeadingBudgetExceededError):
        pdf.raw_bookmark_matches(outline, lines)
