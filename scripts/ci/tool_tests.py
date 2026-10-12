import os
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

from .secret_scan import scan


class PinnedToolTests(unittest.TestCase):
    def test_secret_additions_cannot_be_suppressed_and_removal_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", directory, *args], text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "Test Developer")
            git("config", "user.email", "test@example.com")
            (root / "README.md").write_text("Base\n")
            git("add", ".")
            git("commit", "-qm", "test(ci): create base")
            base = git("rev-parse", "HEAD")
            # Assemble a synthetic fixture so the proof does not itself publish a key-shaped value.
            sentinel = "AKIA" + "QWERTYUIOPASDFGH"
            (root / "credentials.txt").write_text(f"aws_access_key_id = {sentinel}\n")
            git("add", ".")
            git("commit", "-qm", "test(ci): add sentinel")
            leaked = git("rev-parse", "HEAD")
            binary = Path(os.environ["GITLEAKS_BIN"])
            self.assertEqual(scan(root, base, leaked, binary), 1)
            (root / "credentials.txt").write_text(
                f"aws_access_key_id = {sentinel} # gitleaks:allow\n"
            )
            git("add", ".")
            git("commit", "-qm", "test(ci): annotate sentinel")
            annotated = git("rev-parse", "HEAD")
            self.assertEqual(scan(root, base, annotated, binary), 1)
            (root / ".gitleaksignore").write_text(":aws-access-token:2\n")
            git("add", ".")
            git("commit", "-qm", "test(ci): add ignore fingerprint")
            suppressed = git("rev-parse", "HEAD")
            with chdir(root):
                self.assertEqual(scan(root, base, suppressed, binary), 1)
            (root / "credentials.txt").unlink()
            git("add", "-u")
            git("commit", "-qm", "test(ci): remove sentinel")
            self.assertEqual(
                scan(root, suppressed, git("rev-parse", "HEAD"), binary), 0
            )

    def test_only_exact_synthetic_tree_values_are_allowed(self):
        config = Path(__file__).with_name("gitleaks.toml").resolve()
        fixtures = (
            "sk-" + "test-1234567890",
            "sk-" + "second-0987654321",
            "sk-" + "live-9f3c2b71aa4d8e60",
            "ghp_" + "0123456789abcdef",
            "LOGS_INGEST_PER_MINUTE" + "=120",
        )
        with tempfile.TemporaryDirectory() as directory:
            for fixture in fixtures:
                for suffix, expected in (("", 0), ("x", 1)):
                    with self.subTest(fixture=fixture, suffix=suffix):
                        result = subprocess.run(
                            [
                                os.environ["GITLEAKS_BIN"],
                                "stdin",
                                "--no-banner",
                                "--redact=100",
                                "--config",
                                str(config),
                            ],
                            input=f'api_key = "{fixture}{suffix}"\n',
                            text=True,
                            capture_output=True,
                            cwd=directory,
                        )
                        self.assertEqual(result.returncode, expected)

    def test_workflow_typo_and_untrusted_expression_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = Path(directory) / "unsafe.yml"
            workflow.write_text(
                "name: Proof\non: pull_request\npermissions: {}\njobs:\n  proof:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo '${{ github.event.pull_request.title }}'\n"
            )
            result = subprocess.run(
                [
                    os.environ["ZIZMOR_BIN"],
                    "--offline",
                    "--no-progress",
                    "--no-config",
                    str(workflow),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("template-injection", result.stdout + result.stderr)
            workflow.write_text(
                "name: Proof\non: pull_request\njobs:\n  proof:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo '${{ github.missing_property }}'\n"
            )
            result = subprocess.run(
                [
                    os.environ["ACTIONLINT_BIN"],
                    "-shellcheck=",
                    "-pyflakes=",
                    str(workflow),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing_property", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
