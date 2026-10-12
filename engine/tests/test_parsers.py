from __future__ import annotations

from typing import Any

import pytest

from co_scientist.platform.llm import coerce_json_list


@pytest.mark.parametrize(
    ("value", "kwargs", "expected"),
    [
        ([{"a": 1}, "x", {"a": 2}], {"element": "dict"}, [{"a": 1}, {"a": 2}]),
        ({"a": 1}, {"element": "dict"}, [{"a": 1}]),
        (
            {"items": [{"a": 1}]},
            {"element": "dict", "keys": ("items",)},
            [{"a": 1}],
        ),
        (None, {"element": "dict"}, []),
        ("just a string", {"element": "dict"}, []),
        ("single query", {"element": "str"}, ["single query"]),
        ("   ", {"element": "str"}, []),
        ([" a ", "", "b", 42, None], {"element": "str"}, ["a", "b"]),
    ],
)
def test_coerce_json_list_keeps_only_well_typed_elements(
    value: Any, kwargs: dict[str, Any], expected: list[Any]
) -> None:
    assert coerce_json_list(value, site="s", **kwargs) == expected
