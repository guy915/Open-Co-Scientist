from co_scientist.platform.retrieval.research import string_items, stripped_string_items


def test_string_items_keep_every_string_exactly() -> None:
    assert string_items([None, " a ", "", "b", 3]) == [" a ", "", "b"]


def test_stripped_string_items_trim_and_drop_blanks() -> None:
    assert stripped_string_items([None, " a ", "", "  ", "b", 3]) == ["a", "b"]


def test_non_lists_yield_no_items() -> None:
    assert string_items("ab") == []
    assert stripped_string_items({"a": "b"}) == []
