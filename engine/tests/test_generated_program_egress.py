from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from co_scientist.platform.sandbox import command_lifecycle_available, sandbox_backend
from co_scientist.platform.sandbox.workspace import RUN_COMMAND, WorkspaceToolProvider
from co_scientist.platform.sandbox.workspace.run_workspace import open_draft_workspace


@pytest.mark.skipif(
    sandbox_backend() is None or not command_lifecycle_available(),
    reason="generated program requires real confinement and lifecycle isolation",
)
@pytest.mark.asyncio
async def test_generated_program_cannot_connect_to_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COSCIENTIST_WORKSPACE_DIR", str(tmp_path))
    workspace = open_draft_workspace("synthetic-run", "synthetic-draft")
    with socket.socket() as canary:
        canary.bind(("127.0.0.1", 0))
        canary.listen(1)
        port = canary.getsockname()[1]
        code = (
            "import socket; "
            f"s=socket.create_connection(('127.0.0.1',{port}),timeout=1); "
            "print('network-exposed'); s.close()"
        )
        provider = WorkspaceToolProvider(workspace)
        message = await provider.execute_tool_call(
            SimpleNamespace(
                id="call_run_command",
                function=SimpleNamespace(
                    name=RUN_COMMAND,
                    arguments=json.dumps(
                        {"argv": [sys.executable, "-c", code], "yield_seconds": 5}
                    ),
                ),
            )
        )
        outcome = json.loads(message["content"])
    assert outcome["exit_code"] != 0
    assert "network-exposed" not in outcome["stdout"]
    await workspace.close()
