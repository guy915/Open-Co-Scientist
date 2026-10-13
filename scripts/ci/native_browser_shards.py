import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


# Seconds per case measured on real WebKit/iPhone/Firefox jobs. Unknown future
# files use the slowest measured category and always participate in discovery.
CASE_SECONDS = {
    "progress-keyboard.spec.ts": 12,
    "announcements.spec.ts": 13,
    "keyboard.spec.ts": 5,
    "semantics.spec.ts": 3,
}
PROJECTS = ("webkit", "webkit-iphone", "firefox")


def partition_files(report, project, shards=2):
    if shards not in (2, 3):
        raise ValueError("Native sharding supports two or three bins")
    if not isinstance(report, dict) or report.get("errors"):
        raise ValueError("Native test collection failed")
    cases = {}
    seen = set()

    def visit(suites):
        if not isinstance(suites, list):
            raise ValueError("Invalid native suite collection")
        for suite in suites:
            if not isinstance(suite, dict):
                raise ValueError("Invalid native suite")
            for spec in suite.get("specs", []):
                if not isinstance(spec, dict) or not isinstance(
                    spec.get("tests"), list
                ):
                    raise ValueError("Invalid native spec")
                for test in spec["tests"]:
                    if not isinstance(test, dict) or not isinstance(
                        test.get("projectName"), str
                    ):
                        raise ValueError("Invalid native project")
                    if test["projectName"] != project:
                        continue
                    name, identity = spec.get("file"), spec.get("id")
                    if (
                        not isinstance(name, str)
                        or not name
                        or "\\" in name
                        or "\0" in name
                    ):
                        raise ValueError("Unsafe native test path")
                    path = PurePosixPath(name)
                    if path.is_absolute() or ".." in path.parts or str(path) != name:
                        raise ValueError("Unsafe native test path")
                    if (
                        not isinstance(identity, str)
                        or not identity
                        or identity in seen
                    ):
                        raise ValueError("Missing or duplicate native test identity")
                    seen.add(identity)
                    cases.setdefault(name, set()).add(identity)
            visit(suite.get("suites", []))

    visit(report.get("suites"))
    if len(cases) < shards:
        raise ValueError("Every native shard needs selected whole files")
    weights = {
        name: len(ids) * CASE_SECONDS.get(name, 13) for name, ids in cases.items()
    }
    bins, totals = [[] for _ in range(shards)], [0] * shards
    for name in sorted(cases, key=lambda name: (-weights[name], name)):
        selected = min(range(shards), key=lambda index: (totals[index], index))
        bins[selected].append(name)
        totals[selected] += weights[name]
    return [sorted(group) for group in bins]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", choices=PROJECTS, required=True)
    parser.add_argument("--shard", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--shards", type=int, choices=(2, 3), default=2)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.shard > args.shards:
        parser.error("The shard index must not exceed the shard count")
    root = Path(__file__).resolve().parents[2]
    e2e = root / "e2e"
    config = "playwright.cross-browser.config.ts"
    collected = subprocess.run(
        [
            "node",
            "node_modules/@playwright/test/cli.js",
            "test",
            "--config",
            config,
            "--project",
            args.project,
            "--list",
            "--reporter=json",
        ],
        cwd=e2e,
        capture_output=True,
        text=True,
        check=False,
    )
    if collected.returncode:
        print("Native guard collection failed", file=sys.stderr)
        print(collected.stderr, file=sys.stderr)
        return collected.returncode
    try:
        files = partition_files(
            json.loads(collected.stdout), args.project, args.shards
        )[args.shard - 1]
    except (TypeError, ValueError):
        print("Native guard collection is invalid", file=sys.stderr)
        return 1
    print(
        f"Native {args.project} shard {args.shard}/{args.shards} whole files: {', '.join(files)}",
        flush=True,
    )
    patterns = ["/production/" + re.escape(name) + "$" for name in files]
    command = ["node", "e2e/run.mjs", *patterns, "--config", config]
    if args.list:
        command += ["--list", "--reporter=json"]
    # Playwright's variadic project option must follow the file filters.
    command += ["--project", args.project]
    return subprocess.run(
        command,
        cwd=root,
        check=False,
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
