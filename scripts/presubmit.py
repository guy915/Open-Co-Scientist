from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from scripts.select_targets import changed_paths, load_rules, select

TARGET_COMMANDS = {
    "python_lint": ("lint-python",),
    "engine": ("typecheck", "arch", "test-engine"),
    "app": ("typecheck", "arch", "test-app"),
    "frontend": ("lint-frontend", "test-frontend", "build-checked"),
    "evaluations": ("typecheck", "arch", "test-evaluations", "eval-smoke"),
    "mcp_server": ("test-mcp",),
    "e2e": ("e2e", "e2e-production"),
    "docker": ("docker-build",),
    "root_config": ("setup", "lint", "typecheck", "root-config"),
    "workflows": ("lint-workflows",),
    "sandbox": ("test-engine",),
    "cross_browser": ("e2e", "e2e-production"),
}
ORDER = (
    "setup",
    "ci-guards",
    "lint",
    "lint-python",
    "lint-frontend",
    "typecheck",
    "arch",
    "root-config",
    "lint-workflows",
    "test-engine",
    "test-app",
    "test-mcp",
    "test-evaluations",
    "eval-smoke",
    "test-frontend",
    "build-checked",
    "e2e",
    "e2e-production",
    "docker-build",
)


def commands_for(selected: dict[str, bool]) -> list[str]:
    unknown = selected.keys() - TARGET_COMMANDS.keys()
    if unknown:
        raise ValueError(f"No local equivalent for targets: {sorted(unknown)}")
    commands = {"ci-guards"} | {
        command
        for target, affected in selected.items()
        if affected
        for command in TARGET_COMMANDS[target]
    }
    if "lint" in commands:
        commands -= {"lint-python", "lint-frontend", "arch"}
    elif "lint-python" in commands:
        commands.discard("arch")
    return [command for command in ORDER if command in commands]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    selected = select(changed_paths(root, args.base), load_rules(root))
    commands = commands_for(selected)
    print(
        json.dumps({"selected": selected, "commands": commands}, sort_keys=True),
        flush=True,
    )
    if args.dry_run:
        return
    env = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    started = time.monotonic()
    for command in commands:
        before = time.monotonic()
        result = subprocess.run(["make", command], cwd=root, env=env)
        print(
            f"{command}: exit {result.returncode}, {time.monotonic() - before:.2f}s",
            flush=True,
        )
        if result.returncode:
            raise SystemExit(result.returncode)
    print(f"presubmit: {time.monotonic() - started:.2f}s", flush=True)


if __name__ == "__main__":
    main()
