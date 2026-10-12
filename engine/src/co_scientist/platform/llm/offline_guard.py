from __future__ import annotations

from co_scientist.core import byok_scope
from co_scientist.platform.llm.process_mode import offline_mode

__all__ = ["OfflineModeError", "remote_chat_allowed", "require_remote_chat"]


class OfflineModeError(RuntimeError):
    """An app-side provider request was refused by forced offline admission."""


def remote_chat_allowed() -> bool:
    """Forced offline withholds deployment credentials; explicitly scoped
    validated BYOK credentials remain caller-authorized.
    """
    if byok_scope.current_byok() is not None:
        return True
    return not offline_mode()


def require_remote_chat(caller: str) -> None:
    if remote_chat_allowed():
        return
    raise OfflineModeError(f"{caller} uses an explicit deterministic test adapter")
