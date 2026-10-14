import json
import os
import subprocess
import sys
from pathlib import Path

# The uvicorn CLI applies its own stderr logging config before importing the
# app; reproduce that order in a fresh interpreter.
_SCRIPT = """
import logging, logging.config, warnings
from uvicorn.config import LOGGING_CONFIG
logging.config.dictConfig(LOGGING_CONFIG)
import mcp_server.server
logging.getLogger("uvicorn.error").info("Application startup complete.")
logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "peer", "GET", "/", "1.1", 200)
logging.getLogger("fastmcp").warning("fastmcp diagnostic")
logging.getLogger("mcp_server.tools").error("source failed")
warnings.warn("library deprecation", DeprecationWarning, stacklevel=1)
"""


def test_server_logs_json_to_stdout_and_nothing_to_stderr(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "COSCIENTIST_MCP_ALLOW_UNAUTHENTICATED_LOCAL": "1",
        "PYTHONWARNINGS": "always",
    }
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        timeout=120,
        check=True,
    )

    assert result.stderr == ""
    records = [json.loads(line) for line in result.stdout.splitlines()]
    levels = {(r["logger"], r["message"]): r["level"] for r in records}
    assert levels[("uvicorn.error", "Application startup complete.")] == "INFO"
    assert levels[("uvicorn.access", "MCP HTTP request method=GET status=200")] == "INFO"
    assert levels[("fastmcp", "MCP transport diagnostic (private details omitted)")] == "WARNING"
    assert levels[("mcp_server.tools", "source failed")] == "ERROR"
    assert any(r["logger"] == "py.warnings" and r["level"] == "WARNING" for r in records)
    assert not any(".env" in r["message"] and r["level"] != "INFO" for r in records)
