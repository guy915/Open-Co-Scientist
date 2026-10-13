import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci.secret_scan import added_lines, scan


class SecretScanTests(unittest.TestCase):
    def test_only_added_lines_are_scanned(self):
        diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old secret\n+new secret\n context\n"
        self.assertEqual(added_lines(diff), "new secret\n")

    def test_scans_branch_diff_without_shell_and_redacts_detector_output(self):
        base, head = "a" * 40, "b" * 40
        with (
            patch(
                "scripts.ci.secret_scan.subprocess.check_output",
                side_effect=[base + "\n", "@@ -0,0 +1 @@\n+new secret\n"],
            ),
            patch(
                "scripts.ci.secret_scan.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1),
            ) as run,
        ):
            self.assertEqual(scan(Path("."), base, head, Path("/tmp/gitleaks")), 1)
            args = run.call_args.args[0]
            self.assertIn("--redact=100", args)
            self.assertEqual(run.call_args.kwargs["input"], "new secret\n")

    def test_detector_error_is_not_treated_as_clean(self):
        with (
            patch(
                "scripts.ci.secret_scan.subprocess.check_output",
                side_effect=["a" * 40 + "\n", "@@ -0,0 +1 @@\n+value\n"],
            ),
            patch(
                "scripts.ci.secret_scan.subprocess.run",
                return_value=subprocess.CompletedProcess([], 2),
            ),
        ):
            self.assertEqual(
                scan(Path("."), "a" * 40, "b" * 40, Path("/tmp/gitleaks")), 2
            )

    def test_added_text_resembling_a_diff_header_is_still_scanned(self):
        self.assertEqual(added_lines("+++ b/a\n@@ -0,0 +1 @@\n+++ key\n"), "++ key\n")


if __name__ == "__main__":
    unittest.main()
