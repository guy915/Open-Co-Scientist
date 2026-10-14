import json
import os
import sys


SELECTIONS = {
    "format-lint": ("python_lint", "mcp_server"),
    "typecheck": ("engine", "app", "evaluations", "root_config"),
    "test-engine": ("engine",),
    "test-app": ("app",),
    "evaluations": ("evaluations",),
    "frontend": ("frontend",),
    "e2e": ("e2e",),
    "docker-build": ("docker",),
    "workflow-lint": ("workflows",),
    "cross-browser": ("cross_browser",),
    "sandbox-macos": ("sandbox",),
}
EVENTS = {"pull_request", "push", "schedule", "workflow_dispatch", "workflow_call"}


def failures(jobs, event):
    if not isinstance(jobs, dict) or event not in EVENTS:
        return ["invalid-results"]
    failed = {
        name
        for name, job in jobs.items()
        if not isinstance(job, dict) or job.get("result") not in {"success", "skipped"}
    }
    changes = jobs.get("changes", {})
    if not isinstance(changes, dict) or changes.get("result") != "success":
        return sorted(failed | {"changes"})
    outputs = changes.get("outputs", {})
    if not isinstance(outputs, dict):
        return sorted(failed | {"changes"})

    def expect(name, result):
        job = jobs.get(name)
        if not isinstance(job, dict) or job.get("result") != result:
            failed.add(name)

    expect("launch-checks", "success")
    expect("dependency-review", "success" if event == "pull_request" else "skipped")
    expect("cross-browser-full", "skipped" if event == "pull_request" else "success")
    for name, targets in SELECTIONS.items():
        values = [outputs.get(target) for target in targets]
        if any(value not in {"true", "false"} for value in values):
            failed.add("changes")
            continue
        selected = "true" in values
        if name == "sandbox-macos" and event == "push":
            selected = False
        if name == "cross-browser" and event != "pull_request":
            selected = False
        expect(name, "success" if selected else "skipped")
    return sorted(failed)


def main():
    try:
        failed = failures(
            json.loads(os.environ["RESULTS"]), os.environ["GITHUB_EVENT_NAME"]
        )
    except (KeyError, TypeError, ValueError):
        print("Required checks failed: invalid input")
        return 1
    if failed:
        print("Required checks failed: " + ", ".join(failed))
        return 1
    print("All selected checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
