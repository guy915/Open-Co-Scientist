import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mcp_server.framework_log_privacy import FrameworkLogPrivacy
from mcp_server.log_format import JsonFormatter
from uvicorn.logging import AccessFormatter, DefaultFormatter

_PRIVATE = "synthetic-private-research-query"
_EXCEPTION = "synthetic-private-exception-detail"


@pytest.mark.parametrize(
    ("name", "message", "args", "expected"),
    [
        (
            "uvicorn.access",
            "%s - %s %s HTTP/%s %s",
            ("peer", "GET", _PRIVATE, "1.1", 200),
            "MCP HTTP request method=GET status=200",
        ),
        ("uvicorn.access", _PRIVATE, (_PRIVATE,), "MCP HTTP request method=UNKNOWN status=UNKNOWN"),
        (
            "uvicorn.access",
            _PRIVATE,
            ("peer", _PRIVATE, _PRIVATE, "1.1", True),
            "MCP HTTP request method=UNKNOWN status=UNKNOWN",
        ),
        ("uvicorn.error", _PRIVATE, (), "MCP server diagnostic (private details omitted)"),
        ("uvicorn.access.child", _PRIVATE, (), "MCP server diagnostic (private details omitted)"),
    ],
)
def test_framework_records_keep_severity_without_private_details(
    name: str, message: str, args: tuple[object, ...], expected: str
) -> None:
    record = logging.LogRecord(name, logging.ERROR, __file__, 1, message, args, None)
    record.exc_info = (ValueError, ValueError(_EXCEPTION), None)
    record.exc_text = _EXCEPTION
    record.stack_info = _PRIVATE

    assert FrameworkLogPrivacy().filter(record)
    assert FrameworkLogPrivacy().filter(record)
    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "ERROR"
    assert payload["message"] == expected
    assert "exc_info" not in payload
    assert _PRIVATE not in json.dumps(payload)
    assert _EXCEPTION not in json.dumps(payload)


def test_safe_lifecycle_and_source_diagnostics_are_preserved() -> None:
    for name, message, args in (
        ("uvicorn.error", "Application startup complete.", ()),
        ("uvicorn.error", "Started server process [%d]", (123,)),
        ("mcp_server.tools", "Source failed", ()),
    ):
        record = logging.LogRecord(name, logging.INFO, __file__, 1, message, args, None)
        original = record.getMessage()
        assert FrameworkLogPrivacy().filter(record)
        assert record.getMessage() == original


def test_colored_framework_formatter_cannot_restore_private_message() -> None:
    record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, _PRIVATE, (), None)
    record.color_message = _EXCEPTION
    assert FrameworkLogPrivacy(keep_access_args=True).filter(record)
    rendered = DefaultFormatter(fmt="%(levelprefix)s %(message)s", use_colors=True).format(record)
    assert _PRIVATE not in rendered
    assert _EXCEPTION not in rendered
    assert "MCP server diagnostic (private details omitted)" in rendered


def test_reconfigured_uvicorn_access_formatter_keeps_its_shape_without_private_fields() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (_PRIVATE, "GET", _PRIVATE, _PRIVATE, 200),
        None,
    )
    privacy = FrameworkLogPrivacy(keep_access_args=True)
    assert privacy.filter(record)
    assert privacy.filter(record)
    formatter = AccessFormatter(
        fmt='%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    )
    assert formatter.format(record) == 'MCP - "GET [private-target] HTTP/unknown" 200 OK'
    assert FrameworkLogPrivacy().filter(record)
    assert json.loads(JsonFormatter().format(record))["message"] == (
        "MCP HTTP request method=GET status=200"
    )


@pytest.mark.parametrize("cli", [True, False], ids=["cli", "programmatic"])
def test_actual_uvicorn_omits_request_targets_and_exception_details(
    tmp_path: Path, cli: bool
) -> None:
    (tmp_path / "privacy_app.py").write_text(
        "from mcp_server.server import app as original_app\n"
        "async def app(scope, receive, send):\n"
        "    if scope['type'] == 'http' and scope['path'] == '/private-failure':\n"
        f"        raise RuntimeError('{_EXCEPTION}')\n"
        "    await original_app(scope, receive, send)\n"
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{Path(__file__).resolve().parents[2]}",
        "COSCIENTIST_MCP_SHARED_SECRET": "synthetic-framework-secret",
        "COSCIENTIST_LIT_REVIEW_DIR": str(tmp_path / "cache"),
        "PYTHON_DOTENV_DISABLED": "1",
    }
    log_file = tmp_path / "server.log"
    command = (
        [
            sys.executable,
            "-m",
            "uvicorn",
            "privacy_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        if cli
        else [
            sys.executable,
            "-c",
            "from privacy_app import app; import uvicorn; "
            f"uvicorn.run(app, host='127.0.0.1', port={port})",
        ]
    )
    with log_file.open("w") as output:
        server = subprocess.Popen(
            command,
            cwd=tmp_path,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            root = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            while True:
                try:
                    with opener.open(root + "/", timeout=1) as response:
                        assert response.status == 200
                    break
                except OSError:
                    assert server.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            with opener.open(root + f"/?query={_PRIVATE}", timeout=2) as response:
                assert response.status == 200
            for request, status in (
                (urllib.request.Request(root + f"/?query={_PRIVATE}", data=b"{}"), 401),
                (urllib.request.Request(root + "/private-failure"), 500),
            ):
                with pytest.raises(urllib.error.HTTPError) as failure:
                    opener.open(request, timeout=2)
                assert failure.value.code == status
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
    text = log_file.read_text()
    assert _PRIVATE not in text
    assert _EXCEPTION not in text
    assert "--- Logging error ---" not in text
    if cli:
        assert "MCP HTTP request method=GET status=200" in text
        assert "MCP HTTP request method=POST status=401" in text
        assert "MCP HTTP request method=GET status=500" in text
        records = [json.loads(line) for line in text.splitlines()]
        assert any(row["logger"] == "uvicorn.error" and row["level"] == "ERROR" for row in records)
    else:
        assert 'MCP - "GET [private-target] HTTP/unknown" 200' in text
        assert 'MCP - "POST [private-target] HTTP/unknown" 401' in text
        assert 'MCP - "GET [private-target] HTTP/unknown" 500' in text
        assert "ERROR:    MCP server diagnostic (private details omitted)" in text
    assert "Application startup complete." in text
