from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from co_scientist.core.exceptions import ProviderAdmissionError

UNAVAILABLE = "No model is available right now"


class AnthropicCreditUnavailableError(ProviderAdmissionError):
    pass


class AnthropicPromptTooLongError(AnthropicCreditUnavailableError):
    pass


@dataclass(frozen=True)
class CreditReservation:
    cycle_start: float
    cycle_end: float
    limit: int
    amount: int
    role: str
    input_bound: int
    output_bound: int
    rates: str


@dataclass(frozen=True)
class CreditRecord:
    input_bound: int
    output_bound: int
    settled: bool
    rates: str


def reserve_credit(
    conn: sqlite3.Connection, receipt: str, quote: CreditReservation, now: float
) -> None:
    blocked = conn.execute(
        "SELECT disabled_until FROM anthropic_credit_state WHERE slot='operator'"
    ).fetchone()
    active = conn.execute(
        "SELECT start FROM anthropic_credit_cycles WHERE start<=? AND end>? ORDER BY start DESC",
        (now, now),
    ).fetchone()
    if (
        not quote.cycle_start <= now < quote.cycle_end
        or (blocked and now < blocked[0])
        or (active and active[0] != quote.cycle_start)
        or not 0 < quote.amount <= quote.limit
        or quote.input_bound <= 0
        or quote.output_bound <= 0
    ):
        raise AnthropicCreditUnavailableError(UNAVAILABLE)
    # Unknown calls may finish in a later grant; their full reserve survives
    # the reset rather than making a second allowance for the same call.
    charged = conn.execute(
        "SELECT COALESCE(SUM(charged_microusd),0) FROM anthropic_credit "
        "WHERE cycle_start=? OR settled=0",
        (quote.cycle_start,),
    ).fetchone()[0]
    if charged + quote.amount > quote.limit:
        raise AnthropicCreditUnavailableError(UNAVAILABLE)
    conn.execute(
        "INSERT OR IGNORE INTO anthropic_credit_cycles (start,end) VALUES (?,?)",
        (quote.cycle_start, quote.cycle_end),
    )
    conn.execute(
        "INSERT INTO anthropic_credit "
        "(id,cycle_start,created_at,role,reserved_microusd,charged_microusd,"
        "input_bound,output_bound,rates) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            receipt,
            quote.cycle_start,
            now,
            quote.role,
            quote.amount,
            quote.amount,
            quote.input_bound,
            quote.output_bound,
            quote.rates,
        ),
    )


def disable_credit(conn: sqlite3.Connection, until: float) -> None:
    conn.execute(
        "INSERT INTO anthropic_credit_state (slot,disabled_until) VALUES ('operator',?) "
        "ON CONFLICT(slot) DO UPDATE SET "
        "disabled_until=MAX(disabled_until,excluded.disabled_until)",
        (until,),
    )


def credit_record(conn: sqlite3.Connection, receipt: str) -> CreditRecord | None:
    row = conn.execute(
        "SELECT input_bound,output_bound,settled,rates FROM anthropic_credit WHERE id=?", (receipt,)
    ).fetchone()
    return CreditRecord(row[0], row[1], bool(row[2]), row[3]) if row else None


def settle_credit_amount(
    conn: sqlite3.Connection,
    receipt: str,
    *,
    charged: int,
    prompt: int,
    output: int,
    cached: int,
    written: int,
    refused: bool | None = None,
) -> None:
    row = conn.execute(
        "SELECT reserved_microusd FROM anthropic_credit WHERE id=?", (receipt,)
    ).fetchone()
    if row is None or not 0 <= charged <= row[0]:
        raise ProviderAdmissionError(UNAVAILABLE)
    conn.execute(
        "UPDATE anthropic_credit SET charged_microusd=?,settled=1,prompt_tokens=?,output_tokens=?,"
        "cached_tokens=?,cache_write_tokens=?,refused=? WHERE id=? AND settled=0",
        (charged, prompt, output, cached, written, refused, receipt),
    )
