from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from evaluations.tests._engine_fake_backend import SCRIPT_PRELUDE


def test_fresh_fake_backend_loads_bundled_header_metadata_without_http() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (
        SCRIPT_PRELUDE
        + """
import httpx, socket
requested = []
def denied_metadata(url, **kwargs):
    requested.append(url)
    raise RuntimeError('metadata HTTP is unavailable')
def denied_socket(*args, **kwargs):
    raise AssertionError('the fake backend must not use the network')
httpx.get = denied_metadata
socket.socket.connect = denied_socket
from litellm import anthropic_beta_headers_manager as headers
loaded = headers.reload_beta_headers_config()
assert loaded['anthropic']
assert loaded == headers.GetAnthropicBetaHeadersConfig.load_local_beta_headers_config()
assert requested == [], requested
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": os.pathsep.join((str(root), str(root / "engine" / "src"))),
            "PYTHON_DOTENV_DISABLED": "1",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
