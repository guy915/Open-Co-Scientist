import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci.git_hygiene import check_commit, check_pr, main


class GitHygieneTests(unittest.TestCase):
    def test_plain_imperative_title_and_scoped_commits_pass(self):
        self.assertEqual(
            check_pr("Guard launch checks", "Validate changes offline."), []
        )
        self.assertEqual(
            check_commit("fix(ci): guard checks\n\nExplain why.", False), []
        )
        self.assertEqual(check_commit("Merge branch 'main'", True), [])
        self.assertEqual(
            check_commit(
                "fix(ci): retain credits\n\nCo-Authored-By: Jane <jane@example.com>",
                False,
            ),
            [],
        )

    def test_ai_names_and_trailers_fail_in_every_metadata_field(self):
        for value in (
            "Claude",
            "cLaUdE Code",
            "Devin",
            "ChatGPT",
            "GitHub Copilot",
            "Codex",
            "Cursor",
            "Windsurf",
            "Co-Authored-By: Claude <bot@example.com>",
            "Claude-Session: 123",
            "Generated with an assistant",
            "Created by AI",
        ):
            with self.subTest(value=value):
                self.assertTrue(check_pr("Guard checks " + value, ""))
                self.assertTrue(check_pr("Guard checks", value))
                self.assertTrue(
                    check_commit("fix(ci): guard checks\n\n" + value, False)
                )
                self.assertTrue(check_commit("Merge branch 'main'\n\n" + value, True))

    def test_conventional_prefix_is_rejected_only_in_pr_title(self):
        for title in (
            "fix: guard checks",
            "fix(ci): guard checks",
            "feat!: guard checks",
            "refactor(ci)!: guard checks",
        ):
            with self.subTest(title=title):
                self.assertTrue(check_pr(title, ""))
        self.assertEqual(check_pr("Guard checks", "fix(ci): example subject"), [])

    def test_malformed_nonmerge_subject_fails(self):
        for message in (
            "Guard checks",
            "fix: guard checks",
            "fix(): guard checks",
            "fix(ci):",
            "fix(ci): ",
            " fix(ci): guard checks",
        ):
            with self.subTest(message=message):
                self.assertTrue(check_commit(message, False))

    def test_shell_metacharacters_are_data(self):
        self.assertEqual(
            check_pr("Guard checks $(touch /tmp/unused)", "`whoami`\n${PATH}"), []
        )

    def test_cli_checks_entire_commit_body_and_excludes_base_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", directory, *args], text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "Test Developer")
            git("config", "user.email", "test@example.com")
            (root / "a").write_text("base")
            git("add", ".")
            git("commit", "-qm", "legacy unscoped subject")
            base = git("rev-parse", "HEAD")
            (root / "a").write_text("change")
            git("commit", "-qam", "fix(ci): guard checks")
            head = git("rev-parse", "HEAD")
            with patch.dict(
                os.environ,
                {
                    "PR_TITLE": "Guard checks",
                    "PR_BODY": "",
                    "BASE_SHA": base,
                    "HEAD_SHA": head,
                },
            ):
                self.assertEqual(main(root), 0)
            with patch.dict(
                os.environ, {"PR_TITLE": "", "BASE_SHA": base, "HEAD_SHA": head}
            ):
                self.assertEqual(main(root, commits_only=True), 0)
            git(
                "commit",
                "--allow-empty",
                "-qm",
                "fix(ci): guard checks\n\nClaude-Session: 123",
            )
            with patch.dict(
                os.environ,
                {
                    "PR_TITLE": "Guard checks",
                    "PR_BODY": "",
                    "BASE_SHA": base,
                    "HEAD_SHA": git("rev-parse", "HEAD"),
                },
            ):
                self.assertEqual(main(root), 1)
                self.assertEqual(main(root, commits_only=True), 1)


if __name__ == "__main__":
    unittest.main()
