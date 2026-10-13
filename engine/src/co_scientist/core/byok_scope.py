from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CustomModelCapabilities:
    context_length: int | None = None
    tool_calling: bool = True
    json_schema: bool = False
    json_object: bool = False
    reasoning: bool = False
    reasoning_can_disable: bool = False


@dataclass(frozen=True)
class ByokCredential:
    """Plaintext keys exist only in scoped memory, never endpoint responses
    or checkpointed state.
    """

    provider: str
    api_key: str
    model: str
    supervisor_model: str | None = None
    # None means the supervisor shares the worker's provider and key.
    supervisor_provider: str | None = None
    supervisor_api_key: str | None = None
    custom_models: dict[str, CustomModelCapabilities] = field(default_factory=dict)

    @property
    def models(self) -> tuple[str, ...]:
        if self.supervisor_model in (None, self.model):
            return (self.model,)
        return (self.model, str(self.supervisor_model))

    @property
    def secrets(self) -> tuple[str, ...]:
        keys = (self.api_key, self.supervisor_api_key)
        return tuple(dict.fromkeys(key for key in keys if key))

    def keys_by_model(self) -> dict[str, str]:
        """Provider-qualified model strings identify which key pays for a
        call, so a mixed-provider run needs no per-call plumbing.
        """
        keys = {self.model: self.api_key}
        if self.supervisor_model:
            keys[self.supervisor_model] = self.supervisor_api_key or self.api_key
        return keys


_current_byok: ContextVar[ByokCredential | None] = ContextVar("cosci_byok", default=None)


def current_byok() -> ByokCredential | None:
    return _current_byok.get()


def redact_credential(text: str, credential: ByokCredential) -> str:
    for secret in credential.secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def redact_byok_text(text: str) -> str:
    credential = current_byok()
    return text if credential is None else redact_credential(text, credential)


@contextlib.contextmanager
def scoped_byok(
    credential: ByokCredential | None,
) -> Iterator[None]:
    if credential is None:
        yield
        return
    token = _current_byok.set(credential)
    try:
        yield
    finally:
        _current_byok.reset(token)


def byok_model_and_key(model: str) -> tuple[str, str | None]:
    """Scoped credentials override model and key for this execution only,
    without mutating deployment defaults.
    """
    credential = current_byok()
    if credential is None:
        return model, None
    return credential.model, credential.api_key


def _redact_log_details(record: logging.LogRecord) -> None:
    if record.exc_info:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    for attribute in ("exc_text", "stack_info"):
        value = getattr(record, attribute)
        if value:
            setattr(record, attribute, redact_byok_text(value))


class ByokRedactionFilter(logging.Filter):
    """Provider libraries can embed keys in errors; stdout and persistence
    both need scoped credential redaction.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if current_byok() is None:
            return True
        try:
            message = record.getMessage()
            redacted = redact_byok_text(message)
            if redacted != message:
                record.msg = redacted
                record.args = None
            _redact_log_details(record)
        except Exception:
            # Credential redaction must never break logging itself.
            return True
        return True
