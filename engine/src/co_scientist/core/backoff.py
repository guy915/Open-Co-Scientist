import random


def jittered_backoff_seconds(
    attempt: int,
    base_seconds: float,
    max_seconds: float | None = None,
) -> float:
    """Jitter prevents throttled callers from recreating a synchronized
    burst.
    """
    ceiling = base_seconds * 2 ** (attempt - 1)
    if max_seconds is not None:
        ceiling = min(ceiling, max_seconds)
    return random.uniform(ceiling / 2, ceiling)
