"""Avoid preexec_fn deadlocks from locks inherited across threaded forks.
Confinement survives exec; every setup failure must exit before the command.
"""

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

from co_scientist.platform.sandbox import landlock, seccomp
from co_scientist.platform.sandbox.policy import SandboxKind, SandboxPolicy

# Distinct helper exit codes distinguish setup failure from the command's own
# failure.
EXIT_BAD_INVOCATION = 121
EXIT_POLICY_REFUSED = 122
EXIT_EXEC_FAILED = 123


def _fail(code: int, message: str) -> NoReturn:
    print(f"confine_exec: {message}", file=sys.stderr)
    raise SystemExit(code)


def policy_to_json(policy: SandboxPolicy) -> str:
    """Paths cross the process boundary as JSON data, never interpolated
    command source.
    """
    return json.dumps(
        {
            "kind": policy.kind.value,
            "writable_roots": [str(root) for root in policy.writable_roots],
            "readable_roots": [str(root) for root in policy.readable_roots],
            "allows_network": policy.allows_network,
        }
    )


def policy_from_json(raw: str) -> SandboxPolicy:
    data = json.loads(raw)
    return SandboxPolicy(
        kind=SandboxKind(data["kind"]),
        writable_roots=tuple(Path(p) for p in data["writable_roots"]),
        readable_roots=tuple(Path(p) for p in data.get("readable_roots", ())),
        network_allowed=bool(data["allows_network"]),
    )


def _refuse_inexpressible(policy: SandboxPolicy) -> None:
    if landlock.can_enforce(policy):
        return
    offending = ", ".join(str(root) for root in landlock.unenforceable_roots(policy))
    _fail(
        EXIT_POLICY_REFUSED,
        "landlock cannot express a writable root containing protected "
        f"metadata ({offending}); rules are additive, so the carve-out "
        "would silently be granted instead",
    )


def _apply(policy: SandboxPolicy) -> None:
    if policy.kind is SandboxKind.DANGER_FULL_ACCESS:
        return

    if not policy.allows_network:
        try:
            seccomp.deny_network()
        except OSError as exc:
            _fail(EXIT_POLICY_REFUSED, f"could not deny the network: {exc}")

    try:
        seccomp.deny_process_inspection()
    except OSError as exc:
        _fail(EXIT_POLICY_REFUSED, f"could not deny cross-process inspection: {exc}")

    _refuse_inexpressible(policy)

    try:
        landlock.restrict_self(policy)
    except OSError as exc:
        _fail(EXIT_POLICY_REFUSED, f"could not apply landlock: {exc}")


def main(argv: list[str]) -> None:
    if "--" not in argv:
        _fail(EXIT_BAD_INVOCATION, "expected <policy-json> -- <command>")
    separator = argv.index("--")
    command = argv[separator + 1 :]
    if separator != 1 or not command:
        _fail(EXIT_BAD_INVOCATION, "expected <policy-json> -- <command>")

    try:
        policy = policy_from_json(argv[0])
    except (ValueError, KeyError, TypeError) as exc:
        _fail(EXIT_BAD_INVOCATION, f"unreadable policy: {exc}")

    _apply(policy)

    try:
        # Use the rebuilt allowlisted PATH, as other backends do; lookup occurs
        # after confinement.
        os.execvp(command[0], command)
    except OSError as exc:
        _fail(EXIT_EXEC_FAILED, f"could not exec {command[0]}: {exc}")


if __name__ == "__main__":
    main(sys.argv[1:])
