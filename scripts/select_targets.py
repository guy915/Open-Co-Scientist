from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen

TEST_TARGETS = {
    "engine",
    "app",
    "frontend",
    "evaluations",
    "mcp_server",
    "e2e",
    "cross_browser",
}


def load_rules(root: Path) -> dict[str, list[str]]:
    data = json.loads((root / ".github/ci_paths.json").read_text())
    if not isinstance(data, dict) or not data:
        raise ValueError("Path rules must be an object")
    rules: dict[str, list[str]] = {}
    for target, patterns in data.items():
        if not isinstance(target, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", target):
            raise ValueError("Invalid target name")
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) for p in patterns
        ):
            raise ValueError(f"Invalid patterns for {target}")
        rules[target] = patterns
    missing = (
        TEST_TARGETS | {"docker", "root_config", "workflows", "sandbox"}
    ) - rules.keys()
    if missing:
        raise ValueError(f"Missing target rules: {sorted(missing)}")
    return rules


def matches(path: str, pattern: str) -> bool:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern[index : index + 3] == "**/":
            parts.append("(?:.*/)?")
            index += 3
        elif pattern[index : index + 2] == "**":
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.fullmatch("".join(parts), path, re.DOTALL) is not None


def select(
    paths: list[str], rules: dict[str, list[str]], full: bool = False
) -> dict[str, bool]:
    selected = {
        target: full
        or any(matches(path, pattern) for path in paths for pattern in patterns)
        for target, patterns in rules.items()
    }
    # An unclassified path must never silently disable all coverage.
    if full or any(
        not any(
            matches(path, pattern)
            for patterns in rules.values()
            for pattern in patterns
        )
        for path in paths
    ):
        return {**dict.fromkeys(rules, True), "python_lint": True}
    selected["python_lint"] = any(
        selected[target] for target in ("engine", "app", "evaluations")
    )
    docs_only = bool(paths) and all(
        path.startswith("docs/") or path.endswith(".md") for path in paths
    )
    if docs_only:
        for target in TEST_TARGETS:
            selected[target] = False
    return selected


def revision(root: Path, ref: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
        ],
        text=True,
    ).strip()


def changed_paths(root: Path, base: str = "origin/main") -> list[str]:
    common = revision(root, base)
    output = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            f"{common}...HEAD",
            "--",
        ]
    )
    return [
        p.decode("utf-8", errors="surrogateescape") for p in output.split(b"\0") if p
    ]


def pull_request_paths(
    repository: str, number: int, token: str, expected: int
) -> list[str] | None:
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise ValueError("Invalid changed-file count")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or number < 1:
        raise ValueError("Invalid repository or pull request")
    # GitHub caps this endpoint at 3,000 files; use comprehensive checks beyond it.
    if expected > 3000:
        return None
    paths: list[str] = []
    count = 0
    for page in range(1, 31):
        request = Request(
            f"https://api.github.com/repos/{repository}/pulls/{number}/files?per_page=100&page={page}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urlopen(request, timeout=30) as response:
            files = json.load(response)
        if not isinstance(files, list):
            raise ValueError("Invalid pull-request file response")
        for entry in files:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("filename"), str
            ):
                raise ValueError("Invalid changed filename")
            paths.append(entry["filename"])
            previous = entry.get("previous_filename")
            if previous is not None:
                if not isinstance(previous, str):
                    raise ValueError("Invalid previous filename")
                paths.append(previous)
        count += len(files)
        if count > expected:
            return None
        if len(files) < 100:
            return paths if count == expected else None
    return None


def event_selection(root: Path, rules: dict[str, list[str]]) -> dict[str, bool]:
    if os.environ["GITHUB_EVENT_NAME"] != "pull_request":
        return select([], rules, full=True)
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if not isinstance(event, dict) or not isinstance(event.get("pull_request"), dict):
        raise ValueError("Invalid pull-request event")
    pr = event["pull_request"]
    paths = pull_request_paths(
        os.environ["GITHUB_REPOSITORY"],
        pr["number"],
        os.environ["GITHUB_TOKEN"],
        pr["changed_files"],
    )
    if paths is None:
        print("File list is truncated; selecting comprehensive checks.")
    return select(paths or [], rules, full=paths is None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", action="store_true")
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    rules = load_rules(root)
    selected = (
        event_selection(root, rules)
        if args.event
        else select(changed_paths(root, args.base), rules)
    )
    if args.event:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            for target, value in selected.items():
                print(f"{target}={str(value).lower()}", file=output)
    print(json.dumps(selected, sort_keys=True))


if __name__ == "__main__":
    main()
