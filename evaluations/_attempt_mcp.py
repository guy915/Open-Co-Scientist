"""Runs the real MCP server with every upstream answered and counted.

Started by `attempt_envelope` in the MCP interpreter (`.venv-mcp`), with the
engine directory whose `mcp_server` package should serve first on the path.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-dir", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--counts", required=True)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.path.insert(0, args.engine_dir)
    from evaluations import _attempt_wire as wire

    counter = wire.AttemptCounter(args.counts)
    wire.pin_dns()
    wire.install_urllib(counter)
    wire.install_httpx(counter, wire.source_response)

    import uvicorn

    app = importlib.import_module("mcp_server.server").app

    served = Path(sys.modules["mcp_server"].__file__ or "").resolve().parent
    if served != Path(args.engine_dir, "mcp_server").resolve():
        raise SystemExit(f"serving the wrong mcp_server: {served}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
