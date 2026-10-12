from __future__ import annotations

from itertools import count
from types import ModuleType, SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

from pytest import MonkeyPatch


def pin_hypothesis_ids(monkeypatch: MonkeyPatch, models: ModuleType, seed: str) -> None:
    """Make a full-run test reproducible without a production ID-factory seam."""
    namespace = uuid5(NAMESPACE_OID, seed)
    ordinals = count(1)
    monkeypatch.setattr(
        models,
        "uuid",
        SimpleNamespace(uuid4=lambda: uuid5(namespace, str(next(ordinals)))),
    )
