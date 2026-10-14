import os
import subprocess
import sys


def main(actionlint: str, zizmor: str) -> int:
    commands = [
        [actionlint, "-shellcheck=", "-pyflakes="],
        [
            zizmor,
            "--offline",
            "--strict-collection",
            "--no-progress",
            "--format",
            "plain",
            ".github",
        ],
    ]
    codes = [subprocess.run(command, check=False).returncode for command in commands]
    print(f"Workflow lint: actionlint exit={codes[0]}, zizmor exit={codes[1]}")
    return int(any(codes))


if __name__ == "__main__":
    sys.exit(
        main(
            os.environ.get("ACTIONLINT_BIN", "actionlint"),
            os.environ.get("ZIZMOR_BIN", "zizmor"),
        )
    )
