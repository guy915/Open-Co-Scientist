from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

UTC_DAY_SECONDS = 86_400


def utc_day(now: float) -> int:
    return int(now // UTC_DAY_SECONDS)


def utc_day_bounds(now: float) -> tuple[float, float]:
    start = float(utc_day(now) * UTC_DAY_SECONDS)
    return start, start + UTC_DAY_SECONDS


class WindowExceededError(ValueError):
    def __init__(self, detail: str, *, retry_after: int) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


@dataclass(frozen=True)
class WindowCapacity:
    used: int
    limit: int
    reset_at: float


def check_capacity(windows: Iterable[WindowCapacity], *, now: float, detail: str) -> None:
    blocked = [window for window in windows if window.limit > 0 and window.used >= window.limit]
    if blocked:
        delay = max(window.reset_at for window in blocked) - now
        raise WindowExceededError(detail, retry_after=max(1, math.ceil(delay)))
