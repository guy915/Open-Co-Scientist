from __future__ import annotations

import calendar
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import connect, current_time, transaction
from co_scientist.platform.db.anthropic_credit import (
    UNAVAILABLE,
    AnthropicCreditUnavailableError,
    CreditRecord,
    CreditReservation,
    disable_credit,
)
from co_scientist.platform.llm.admission.spend import require_enabled
from co_scientist.platform.llm.profile import HAIKU, model_profile
from co_scientist.platform.llm.roles import current_call_policy

_blocked: set[str] = set()
_lock = threading.Lock()


def cycle_bounds(now: float, day: int) -> tuple[float, float]:
    if type(day) is not int or not 1 <= day <= 31:
        raise ProviderAdmissionError(UNAVAILABLE)
    current = datetime.fromtimestamp(now, timezone.utc)

    def boundary(offset: int) -> float:
        months = current.year * 12 + current.month - 1 + offset
        year, month = divmod(months, 12)
        month += 1
        actual_day = min(day, calendar.monthrange(year, month)[1])
        return datetime(year, month, actual_day, tzinfo=timezone.utc).timestamp()

    offset = 0 if now >= boundary(0) else -1
    return boundary(offset), boundary(offset + 1)


@dataclass(frozen=True)
class CreditConfig:
    credit: int
    limit: int
    cycle_start: float
    cycle_end: float


def credit_config() -> CreditConfig:
    require_enabled()
    try:
        credit = Decimal(os.getenv("ANTHROPIC_MONTHLY_CREDIT_USD", "100"))
        if not credit.is_finite() or credit <= 0:
            raise InvalidOperation
        limit = int((credit * 950_000).to_integral_value(rounding=ROUND_FLOOR))
        if not 0 < limit < 2**63:
            raise InvalidOperation
        day = int(os.getenv("ANTHROPIC_BILLING_RESET_DAY", "7"))
        start, end = cycle_bounds(current_time(), day)
    except (InvalidOperation, ValueError) as error:
        raise AnthropicCreditUnavailableError(UNAVAILABLE) from error
    return CreditConfig(int(credit * 1_000_000), limit, start, end)


def require_credit_available(path: str, *, dispatch: bool = False) -> CreditConfig:
    config = credit_config()
    with _lock:
        if path in _blocked:
            raise AnthropicCreditUnavailableError(UNAVAILABLE)
    with connect(path) as conn:
        state = conn.execute(
            "SELECT disabled_until FROM anthropic_credit_state WHERE slot='operator'"
        ).fetchone()
        charged = conn.execute(
            "SELECT COALESCE(SUM(charged_microusd),0) FROM anthropic_credit "
            "WHERE cycle_start=? OR settled=0",
            (config.cycle_start,),
        ).fetchone()[0]
    if (
        (state and current_time() < state[0])
        or charged > config.limit
        or (charged == config.limit and not dispatch)
    ):
        raise AnthropicCreditUnavailableError(UNAVAILABLE)
    return config


def mark_credit_exhausted(path: str) -> None:
    with transaction(path, durable=True) as conn:
        disable_credit(conn, credit_config().cycle_end)


def block_credit(path: str) -> None:
    with _lock:
        _blocked.add(path)


def prepare_credit(request: dict[str, Any], tokens: int, path: str) -> CreditReservation | None:
    if request.get("model") != HAIKU:
        return None
    config = require_credit_available(path)
    output = request.get("max_tokens")
    if type(output) is not int or output <= 0:
        raise ProviderAdmissionError(UNAVAILABLE)
    inputs = tokens - output
    price = model_profile(HAIKU).price
    if price is None or price.long_context is None:
        raise ProviderAdmissionError(UNAVAILABLE)
    # Count-token responses are estimates. Reserve at the higher tier and
    # full cache-write rate; real inclusive usage settles the correct tier.
    high = price.long_context
    amount = int(
        (
            inputs * Decimal(str(high.cache_write_usd_per_million))
            + output * Decimal(str(high.completion_usd_per_million))
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    return CreditReservation(
        config.cycle_start,
        config.cycle_end,
        config.limit,
        amount,
        current_call_policy().role,
        inputs,
        output,
        credit_rates(),
    )


def credit_rates() -> str:
    price = model_profile(HAIKU).price
    if price is None or price.long_context is None:
        raise ProviderAdmissionError(UNAVAILABLE)
    return json.dumps(
        {
            tier: {
                "input": str(item.prompt_usd_per_million),
                "output": str(item.completion_usd_per_million),
                "cached": str(item.cached_prompt_usd_per_million),
                "write": str(item.cache_write_usd_per_million),
            }
            for tier, item in (("short", price), ("long", price.long_context))
        }
    )


def usage_cost(
    prompt: int, output: int, *, cached: int, written: int, rates: str | None = None
) -> int:
    values = (prompt, output, cached, written)
    if any(type(value) is not int or value < 0 for value in values) or cached + written > prompt:
        raise ProviderAdmissionError(UNAVAILABLE)
    price = json.loads(rates if rates is not None else credit_rates())[
        "long" if prompt > 100_000 else "short"
    ]
    amount = (
        (prompt - cached - written) * Decimal(price["input"])
        + cached * Decimal(price["cached"])
        + written * Decimal(price["write"])
        + output * Decimal(price["output"])
    )
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def credit_settlement(
    row: CreditRecord,
    *,
    prompt: int | None,
    output: int | None,
    cached: int | None,
    written: int | None,
) -> tuple[int, int, int, int, int] | None:
    if row.settled:
        return None
    if any(type(value) is not int or value < 0 for value in (prompt, output, cached, written)):
        return None
    assert prompt is not None and output is not None and cached is not None and written is not None
    if prompt > row.input_bound or output > row.output_bound:
        raise ProviderAdmissionError(UNAVAILABLE)
    return (
        usage_cost(prompt, output, cached=cached, written=written, rates=row.rates),
        prompt,
        output,
        cached,
        written,
    )
