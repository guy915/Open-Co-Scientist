from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any

import pytest
from mcp_server.tool_logging import _describe_result, with_call_logging

_PRIVATE = "PRIVATE_GOAL PRIVATE_DOCUMENT person@example.test sk-private-provider-key"


@pytest.mark.parametrize(
    "result",
    [
        {"status": "failed", "error": _PRIVATE, "records": []},
        {"query": _PRIVATE, "records": [{"text": _PRIVATE}]},
        json.dumps({"query": _PRIVATE, "records": [{"text": _PRIVATE}]}),
        _PRIVATE,
        True,
    ],
)
def test_source_logs_keep_result_metadata_without_payloads(
    result: Any, caplog: pytest.LogCaptureFixture
) -> None:
    def tool(query: str, *, document: str) -> Any:
        return result

    wrapped = with_call_logging(tool, "literature_search")
    with caplog.at_level(logging.INFO, logger="mcp_server.tool_logging"):
        assert wrapped(_PRIVATE, document=_PRIVATE) is result
    assert "literature_search(1 positional, 1 named)" in caplog.text
    for token in _PRIVATE.split():
        assert token not in caplog.text
    assert inspect.signature(wrapped) == inspect.signature(tool)


def test_failure_logs_drop_exception_messages_and_tracebacks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def tool(query: str) -> None:
        raise RuntimeError(_PRIVATE)

    wrapped = with_call_logging(tool, "literature_search")
    with (
        caplog.at_level(logging.INFO, logger="mcp_server.tool_logging"),
        pytest.raises(RuntimeError, match="PRIVATE_GOAL"),
    ):
        asyncio.run(wrapped(_PRIVATE))
    assert "raised RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    for token in _PRIVATE.split():
        assert token not in caplog.text
    assert inspect.signature(wrapped) == inspect.signature(tool)


def test_arbitrary_objects_are_not_rendered_in_diagnostics() -> None:
    class PrivateResult:
        def __repr__(self) -> str:
            raise AssertionError("payload repr must not be called")

    assert _describe_result(PrivateResult()) == "scalar"
