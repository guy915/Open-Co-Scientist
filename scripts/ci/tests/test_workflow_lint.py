import subprocess
import unittest
from unittest.mock import patch

from scripts.ci.workflow_lint import main


class WorkflowLintTests(unittest.TestCase):
    def test_both_linters_run_when_first_fails(self):
        with patch(
            "scripts.ci.workflow_lint.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 1),
                subprocess.CompletedProcess([], 0),
            ],
        ) as run:
            self.assertEqual(main("actionlint", "zizmor"), 1)
            self.assertEqual(run.call_count, 2)
            self.assertIn("--offline", run.call_args_list[1].args[0])

    def test_clean_results_pass_and_collection_errors_fail(self):
        for codes, expected in (((0, 0), 0), ((0, 2), 1)):
            with (
                self.subTest(codes=codes),
                patch(
                    "scripts.ci.workflow_lint.subprocess.run",
                    side_effect=[
                        subprocess.CompletedProcess([], code) for code in codes
                    ],
                ),
            ):
                self.assertEqual(main("actionlint", "zizmor"), expected)


if __name__ == "__main__":
    unittest.main()
