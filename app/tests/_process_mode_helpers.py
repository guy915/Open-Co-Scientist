from __future__ import annotations

from collections.abc import Callable


class FakeProcessMode:
    # Offline posture and credential availability are independent knobs in
    # partially configured deployments.

    def __init__(self) -> None:
        self.offline = True
        self.credential: bool | Callable[[str], bool] = False

    def online(self, *, credential: bool | Callable[[str], bool] = True) -> None:
        self.offline = False
        self.credential = credential

    def is_offline(self) -> bool:
        return self.offline

    def credential_available(self, model: str) -> bool:
        if callable(self.credential):
            return self.credential(model)
        return self.credential
