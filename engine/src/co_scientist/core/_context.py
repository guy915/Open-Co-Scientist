from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar

_T = TypeVar("_T")


@contextmanager
def _bind_contextvar(variable: ContextVar[_T], value: _T) -> Iterator[_T]:
    token = variable.set(value)
    try:
        yield value
    finally:
        variable.reset(token)
