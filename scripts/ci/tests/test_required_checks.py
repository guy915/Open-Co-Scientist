import copy
import json
import os
import subprocess
import sys
import unittest

from scripts.ci.required_checks import failures


class RequiredChecksTests(unittest.TestCase):
    def setUp(self):
        self.outputs = dict.fromkeys(
            (
                "python_lint",
                "mcp_server",
                "engine",
                "app",
                "evaluations",
                "root_config",
                "frontend",
                "e2e",
                "docker",
                "workflows",
                "sandbox",
                "cross_browser",
            ),
            "true",
        )
        self.jobs = {
            name: {"result": "success"}
            for name in (
                "changes",
                "format-lint",
                "typecheck",
                "test-engine",
                "test-app",
                "evaluations",
                "frontend",
                "e2e",
                "docker-build",
                "launch-checks",
                "workflow-lint",
                "dependency-review",
                "sandbox-macos",
                "cross-browser",
            )
        }
        self.jobs["changes"]["outputs"] = self.outputs
        self.jobs["cross-browser-full"] = {"result": "skipped"}

    def test_every_selected_check_must_succeed(self):
        self.assertEqual(failures(self.jobs, "pull_request"), [])
        for name in self.jobs.keys() - {"cross-browser-full"}:
            for result in ("failure", "cancelled", "skipped"):
                with self.subTest(name=name, result=result):
                    jobs = copy.deepcopy(self.jobs)
                    jobs[name]["result"] = result
                    self.assertIn(name, failures(jobs, "pull_request"))

    def test_missing_results_and_selections_fail_closed(self):
        for name in self.jobs:
            jobs = copy.deepcopy(self.jobs)
            jobs.pop(name)
            with self.subTest(missing_job=name):
                self.assertTrue(failures(jobs, "pull_request"))
        for target in self.outputs:
            jobs = copy.deepcopy(self.jobs)
            jobs["changes"]["outputs"].pop(target)
            with self.subTest(missing_selection=target):
                self.assertIn("changes", failures(jobs, "pull_request"))

    def test_unselected_checks_skip_but_launch_and_dependency_checks_run(self):
        jobs = copy.deepcopy(self.jobs)
        jobs["changes"]["outputs"] = dict.fromkeys(self.outputs, "false")
        for name in jobs.keys() - {"changes", "launch-checks", "dependency-review"}:
            jobs[name]["result"] = "skipped"
        self.assertEqual(failures(jobs, "pull_request"), [])
        for name in ("launch-checks", "dependency-review"):
            broken = copy.deepcopy(jobs)
            broken[name]["result"] = "skipped"
            self.assertIn(name, failures(broken, "pull_request"))

    def test_postsubmit_skips_pr_review_and_macos_but_nightly_requires_macos(self):
        jobs = copy.deepcopy(self.jobs)
        jobs["dependency-review"]["result"] = "skipped"
        jobs["sandbox-macos"]["result"] = "skipped"
        jobs["cross-browser"]["result"] = "skipped"
        jobs["cross-browser-full"]["result"] = "success"
        self.assertEqual(failures(jobs, "push"), [])
        for event in ("schedule", "workflow_dispatch", "workflow_call"):
            self.assertIn("sandbox-macos", failures(jobs, event))
            jobs["sandbox-macos"]["result"] = "success"
            self.assertEqual(failures(jobs, event), [])
            jobs["sandbox-macos"]["result"] = "skipped"

    def test_native_call_must_match_the_event_even_when_other_call_succeeds(self):
        for event in (
            "pull_request",
            "push",
            "schedule",
            "workflow_dispatch",
            "workflow_call",
        ):
            jobs = copy.deepcopy(self.jobs)
            if event != "pull_request":
                jobs["dependency-review"]["result"] = "skipped"
                jobs["cross-browser"]["result"] = "skipped"
                jobs["cross-browser-full"]["result"] = "success"
            if event == "push":
                jobs["sandbox-macos"]["result"] = "skipped"
            self.assertEqual(failures(jobs, event), [])
            for name in ("cross-browser", "cross-browser-full"):
                broken = copy.deepcopy(jobs)
                broken[name]["result"] = (
                    "skipped" if jobs[name]["result"] == "success" else "success"
                )
                with self.subTest(event=event, wrong_call=name):
                    self.assertIn(name, failures(broken, event))
        jobs = copy.deepcopy(self.jobs)
        jobs["changes"]["outputs"]["cross_browser"] = "false"
        jobs["cross-browser"]["result"] = "skipped"
        self.assertEqual(failures(jobs, "pull_request"), [])
        jobs["cross-browser-full"]["result"] = "success"
        self.assertIn("cross-browser-full", failures(jobs, "pull_request"))

    def test_invalid_outputs_payloads_and_unknown_events_fail(self):
        for value in (None, True, "", "yes", 1):
            jobs = copy.deepcopy(self.jobs)
            jobs["changes"]["outputs"]["sandbox"] = value
            with self.subTest(value=value):
                self.assertIn("changes", failures(jobs, "push"))
        for payload in (None, [], {}, {"changes": None}):
            self.assertTrue(failures(payload, "pull_request"))
        self.assertTrue(failures(self.jobs, "merge_group"))

    def test_a_new_failed_dependency_is_never_ignored(self):
        self.jobs["new-guard"] = {"result": "failure"}
        self.assertIn("new-guard", failures(self.jobs, "pull_request"))

    def test_cli_rejects_failed_guards_and_malformed_payloads(self):
        broken = copy.deepcopy(self.jobs)
        broken["cross-browser"]["result"] = "skipped"
        for payload, expected in (
            (json.dumps(self.jobs), 0),
            (json.dumps(broken), 1),
            ("{", 1),
            (json.dumps({"changes": {"result": []}}), 1),
        ):
            with self.subTest(payload=payload):
                result = subprocess.run(
                    [sys.executable, "-m", "scripts.ci.required_checks"],
                    env={
                        **os.environ,
                        "RESULTS": payload,
                        "GITHUB_EVENT_NAME": "pull_request",
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
