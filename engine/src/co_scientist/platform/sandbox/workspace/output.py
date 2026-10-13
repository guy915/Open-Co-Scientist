"""Redact inline/stored output; drop it if a registered secret survives.
Host-side spills must resist symlinks on every confinement backend.
"""

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from co_scientist.platform.sandbox.patch import PatchError, write_workspace_file
from co_scientist.platform.sandbox.policy import HARNESS_METADATA_NAME

logger = logging.getLogger(__name__)

# Masking short coincidental values corrupts ordinary output; reject their
# registration.
MIN_SECRET_LENGTH = 8

SPILL_DIRECTORY = f"{HARNESS_METADATA_NAME}/output"

# Inline previews preserve traceback context without consuming the reasoning
# window.
DEFAULT_PREVIEW_CHARS = 16_000

# Match credential-shaped environment names to catch variables not explicitly
# listed.
_SECRET_NAME_PATTERN = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)


class SecretRegistrationError(ValueError):
    """Refusing unmaskable values prevents callers mistaking corruption for
    protection.
    """


class SecretRegistry:
    def __init__(self) -> None:
        # One value may have several names; the first registration supplies its
        # label.
        self._names_by_value: dict[str, str] = {}

    def register(self, name: str, value: str) -> None:
        if len(value) < MIN_SECRET_LENGTH:
            raise SecretRegistrationError(
                f"refusing to register {name!r}: a value shorter than "
                f"{MIN_SECRET_LENGTH} characters cannot be masked without "
                "corrupting output that merely contains it"
            )
        self._names_by_value.setdefault(value, name)

    def register_environment(self, environ: dict[str, str] | None = None) -> tuple[str, ...]:
        source = os.environ if environ is None else environ
        registered = []
        for name, value in source.items():
            if not value or not _SECRET_NAME_PATTERN.search(name):
                continue
            try:
                self.register(name, value)
            except SecretRegistrationError as exc:
                logger.warning("%s", exc)
                continue
            registered.append(name)
        return tuple(registered)

    def redact(self, text: str) -> str:
        """Longest-first replacement prevents a containing secret being half-
        masked inside.
        """
        for value in sorted(self._names_by_value, key=len, reverse=True):
            if value in text:
                text = text.replace(value, f"[redacted:{self._names_by_value[value]}]")
        return text

    def survivors(self, text: str) -> tuple[str, ...]:
        return tuple(sorted(name for value, name in self._names_by_value.items() if value in text))


@dataclass(frozen=True)
class OutputPointer:
    path: str
    digest: str
    total_chars: int


@dataclass(frozen=True)
class BoundedOutput:
    text: str
    truncated: bool
    pointer: OutputPointer | None


def _preview(text: str, limit: int) -> str:
    """Keep both ends: verdicts and tracebacks appear at the tail, context at
    the head.
    """
    omitted = len(text) - limit
    head = (limit * 2) // 3
    tail = limit - head
    return (
        f"{text[:head]}\n"
        f"[... {omitted} characters omitted; read the full output with "
        f"read_file ...]\n"
        f"{text[-tail:]}"
    )


class OutputRecorder:
    def __init__(
        self,
        root: Path,
        secrets: SecretRegistry | None = None,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ) -> None:
        self.root = root.resolve()
        self.secrets = secrets if secrets is not None else SecretRegistry()
        self._preview_chars = preview_chars

    def record(self, label: str, text: str) -> BoundedOutput:
        if not text:
            return BoundedOutput(text="", truncated=False, pointer=None)

        redacted = self.secrets.redact(text)
        leaked = self.secrets.survivors(redacted)
        if leaked:
            logger.error("dropping %s: redaction did not remove %s", label, leaked)
            return BoundedOutput(
                text=(f"[output withheld: it still contained {', '.join(leaked)} after redaction]"),
                truncated=True,
                pointer=None,
            )

        if len(redacted) <= self._preview_chars:
            return BoundedOutput(text=redacted, truncated=False, pointer=None)

        return BoundedOutput(
            text=_preview(redacted, self._preview_chars),
            truncated=True,
            pointer=self._spill(label, redacted),
        )

    def _spill(self, label: str, redacted: str) -> OutputPointer | None:
        """A full disk may lose output's middle, never the command."""
        digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
        relative = f"{SPILL_DIRECTORY}/{label}-{digest[:12]}.txt"
        try:
            write_workspace_file(self.root, relative, redacted)
        except (OSError, PatchError) as exc:
            logger.warning("could not spill %s output: %s", label, exc)
            return None
        return OutputPointer(path=relative, digest=digest, total_chars=len(redacted))
