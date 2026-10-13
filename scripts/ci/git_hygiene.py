import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


AI_TOOL = re.compile(
    r"\b(?:claude|devin|chatgpt|copilot|codex|cursor|windsurf|aider|"
    r"gemini(?:\s+cli)?|amazon\s+q|codewhisperer|tabnine|replit\s+agent)\b",
    re.IGNORECASE,
)
AI_TRAILER = re.compile(
    r"claude[-_ ]session\s*:|generated\s+with\b|created\s+by\s+(?:an?\s+)?AI\b|"
    r"co-authored-by\s*:[^\n]*(?:\bAI\b|\bartificial intelligence\b)",
    re.IGNORECASE,
)
COMMIT_SUBJECT = re.compile(r"[a-z][a-z0-9-]*\([^\s():]+\)!?: \S.*")
PR_PREFIX = re.compile(r"^[a-z][a-z0-9-]*(?:\([^\r\n()]+\))?!?:\s", re.IGNORECASE)


def metadata_findings(text: str) -> list[str]:
    findings = []
    if AI_TOOL.search(text):
        findings.append("tool attribution is forbidden")
    if AI_TRAILER.search(text):
        findings.append("generated attribution/trailer is forbidden")
    return findings


def check_pr(title: str, body: str) -> list[str]:
    findings = [f"PR title: {finding}" for finding in metadata_findings(title)]
    findings += [f"PR body: {finding}" for finding in metadata_findings(body)]
    if not title.strip():
        findings.append("PR title is missing")
    if PR_PREFIX.search(title.lstrip()):
        findings.append(
            "PR title must be an imperative summary without a commit prefix"
        )
    return findings


def check_commit(message: str, merge: bool) -> list[str]:
    findings = metadata_findings(message)
    subject = message.split("\n", 1)[0]
    if not merge and not COMMIT_SUBJECT.fullmatch(subject):
        findings.append("non-merge subject must follow <type>(<scope>): <subject>")
    return findings


def revision(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value):
        raise ValueError("BASE_SHA and HEAD_SHA must be full commit hashes")
    return value


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True)


def main(root: Path = Path("."), *, commits_only: bool = False) -> int:
    findings = (
        []
        if commits_only
        else check_pr(os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""))
    )
    base = revision(os.environ.get("BASE_SHA", ""))
    head = revision(os.environ.get("HEAD_SHA", ""))
    commits = git(root, "rev-list", "--reverse", f"{base}..{head}").splitlines()
    for commit in commits:
        message = git(root, "show", "-s", "--format=%B", commit)
        parents = git(root, "show", "-s", "--format=%P", commit).split()
        findings += [
            f"commit {commit[:12]}: {finding}"
            for finding in check_commit(message, len(parents) > 1)
        ]
    for finding in findings:
        print(finding)
    print(f"Git hygiene: {len(commits)} commits checked, {len(findings)} findings")
    return int(bool(findings))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--commits-only", action="store_true")
    sys.exit(main(commits_only=parser.parse_args().commits_only))
