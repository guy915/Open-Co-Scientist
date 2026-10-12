from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import Connection, connect, current_time, transaction
from co_scientist.platform.db.admission import ProviderReservation
from co_scientist.platform.db.spend import (
    UNAVAILABLE,
    AzureAllowance,
    SpendRecord,
    SpendReservation,
    active_allowance,
    latest_hold,
    ledger_total,
    record_allowance,
)
from co_scientist.platform.llm.profile import ModelPrice, model_profile
from co_scientist.platform.llm.roles import current_call_policy

_blocked: set[str] = set()
_lock = threading.Lock()
_MAX_INTEGER = 2**63 - 1
_run_id: ContextVar[str | None] = ContextVar("spend_run_id", default=None)


@contextmanager
def scoped_run_spending(run_id: str | None) -> Iterator[None]:
    token = _run_id.set(run_id)
    try:
        yield
    finally:
        _run_id.reset(token)


def current_spending_run_id() -> str | None:
    return _run_id.get()


def _enabled(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() in ("1", "true"):
        return True
    if value.lower() in ("0", "false"):
        return False
    raise ProviderAdmissionError(UNAVAILABLE)


def require_enabled() -> None:
    if not _enabled("LLM_ENABLED", True):
        raise ProviderAdmissionError(UNAVAILABLE)


def _decimal(value: str | None) -> Decimal:
    try:
        result = Decimal(value or "")
        if not result.is_finite() or result <= 0:
            raise InvalidOperation
        return result
    except InvalidOperation as error:
        raise ProviderAdmissionError(UNAVAILABLE) from error


def _microeur(value: Decimal) -> int:
    result = int(value.to_integral_value(rounding=ROUND_CEILING))
    if not 0 <= result <= _MAX_INTEGER:
        raise ProviderAdmissionError(UNAVAILABLE)
    return result


@dataclass(frozen=True)
class SpendConfig:
    total: int
    fx: Decimal
    cutoff: float


def parse_expiry(raw: str) -> datetime:
    # A date alone or a naive time leaves the zone to guesswork; the credit
    # lot's expiry must be stated as an instant.
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderAdmissionError(UNAVAILABLE) from error
    if "T" not in raw or moment.tzinfo is None or moment.utcoffset() is None:
        raise ProviderAdmissionError(UNAVAILABLE)
    return moment.astimezone(UTC)


def cutoff_before(expiry: datetime, hours: Decimal) -> float:
    # The margin covers clock skew, requests still in flight and provider
    # usage rated after the instant it was incurred.
    if not hours.is_finite() or not 1 <= hours <= 24 * 366:
        raise ProviderAdmissionError(UNAVAILABLE)
    try:
        return (expiry - timedelta(hours=float(hours))).timestamp()
    except (OverflowError, ValueError) as error:
        raise ProviderAdmissionError(UNAVAILABLE) from error


def azure_config() -> SpendConfig:
    require_enabled()
    if not _enabled("LLM_AZURE_ENABLED", False):
        raise ProviderAdmissionError(UNAVAILABLE)
    total = _decimal(os.getenv("LLM_TOTAL_BUDGET_EUR"))
    # Euro meters follow Microsoft's own price sheet, so there is no safe default.
    fx = _decimal(os.getenv("LLM_USD_TO_EUR"))
    expiry = parse_expiry(os.getenv("LLM_AZURE_EXPIRES_AT", ""))
    try:
        hours = Decimal(os.getenv("LLM_AZURE_CUTOFF_HOURS", "48"))
    except InvalidOperation as error:
        raise ProviderAdmissionError(UNAVAILABLE) from error
    cutoff = cutoff_before(expiry, hours)
    if current_time() >= cutoff:
        raise ProviderAdmissionError(UNAVAILABLE)
    limit = int((total * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
    if not 0 < limit <= _MAX_INTEGER:
        raise ProviderAdmissionError(UNAVAILABLE)
    return SpendConfig(limit, fx, cutoff)


def _anchored(config: SpendConfig, allowance: AzureAllowance | None) -> SpendConfig:
    if allowance is None or not allowance.admits(current_time()):
        raise ProviderAdmissionError(UNAVAILABLE)
    return SpendConfig(
        min(config.total, allowance.allowance), config.fx, min(config.cutoff, allowance.cutoff_at)
    )


def effective_azure_config(conn: Connection) -> SpendConfig:
    return _anchored(azure_config(), active_allowance(conn))


# Luna's published boundary: a request with more input tokens pays the long
# rates for all of it. Input bounds count bytes, which never undercount tokens.
SHORT_CONTEXT_TOKENS = 272_000


def _model_rates(model: str) -> dict[str, str]:
    profile = model_profile(model)
    price = profile.price
    if price is None or profile.version is None:
        raise ProviderAdmissionError(UNAVAILABLE)
    rates = _tier_rates(price.long_context or price)
    if price.long_context is not None:
        rates.update({f"short_{name}": value for name, value in _tier_rates(price).items()})
    return rates


def _tier_rates(price: ModelPrice) -> dict[str, str]:
    # A zero cache-read price means unmeasured, not free.
    cached = price.cached_prompt_usd_per_million or price.prompt_usd_per_million
    return {
        "input": str(price.prompt_usd_per_million),
        "output": str(price.completion_usd_per_million),
        "cached": str(cached),
        "write": str(price.cache_write_usd_per_million),
    }


AZURE_MODELS = ("azure/gpt-6-luna-2026-09-22",)


def verified_rates(fx: Decimal) -> dict[str, Any]:
    return {"fx": str(fx), "models": {model: _model_rates(model) for model in AZURE_MODELS}}


def _money(name: str, value: Decimal, *, positive: bool, rounding: str) -> int:
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(
            f"{name} must be a finite {'positive' if positive else 'non-negative'} amount"
        )
    return int((value * 1_000_000).to_integral_value(rounding=rounding))


def establish_allowance(
    db_path: str,
    *,
    grant_eur: Decimal,
    prior_usage_eur: Decimal,
    buffer_eur: Decimal,
    expires_at: datetime,
    cutoff_hours: Decimal,
    usd_to_eur: Decimal,
    supersedes_version: int | None = None,
    acknowledge_holds: bool = False,
    note: str = "",
) -> AzureAllowance:
    # Round the grant down and every deduction up.
    grant = _money("grant_eur", grant_eur, positive=True, rounding=ROUND_FLOOR)
    prior = _money("prior_usage_eur", prior_usage_eur, positive=False, rounding=ROUND_CEILING)
    buffer = _money("buffer_eur", buffer_eur, positive=True, rounding=ROUND_CEILING)
    allowance = grant - prior - buffer
    if not 0 < allowance <= _MAX_INTEGER:
        raise ValueError("prior usage and buffer leave no allowance")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expires_at must state its UTC offset")
    try:
        cutoff = cutoff_before(expires_at.astimezone(UTC), cutoff_hours)
    except ProviderAdmissionError as error:
        raise ValueError("cutoff_hours must be at least 1") from error
    if cutoff <= current_time():
        raise ValueError("the cutoff has already passed")
    if not usd_to_eur.is_finite() or usd_to_eur <= 0:
        raise ValueError("usd_to_eur must be a finite positive rate")
    rates = verified_rates(usd_to_eur)
    with transaction(db_path, durable=True) as conn:
        active = active_allowance(conn)
        holds = latest_hold(conn) if acknowledge_holds else (active.holds_through if active else 0)
        loosens = active is not None and (
            allowance > active.allowance
            or expires_at.timestamp() > active.expires_at
            or cutoff > active.cutoff_at
            or holds > active.holds_through
        )
        # One token must not silently reopen spend: loosening any limit names
        # the version it replaces.
        if loosens and (active is None or supersedes_version != active.version):
            raise ValueError(
                "raising the allowance, extending expiry or clearing holds "
                "requires supersedes_version set to the active version"
            )
        basis = {
            "grant_eur": str(grant_eur),
            "prior_usage_eur": str(prior_usage_eur),
            "buffer_eur": str(buffer_eur),
            "cutoff_hours": str(cutoff_hours),
            "ledger_at_record_microeur": ledger_total(conn),
            "note": note[:500],
        }
        record_allowance(
            conn,
            allowance=allowance,
            expires_at=expires_at.timestamp(),
            cutoff_at=cutoff,
            rates=json.dumps(rates, sort_keys=True),
            basis=json.dumps(basis, sort_keys=True),
            holds_through=holds,
        )
        recorded = active_allowance(conn)
    if acknowledge_holds:
        # The record re-baselines from provider figures, which also covers a
        # hold this process could not make durable.
        with _lock:
            _blocked.discard(db_path)
    if recorded is None:
        raise ProviderAdmissionError(UNAVAILABLE)
    return recorded


def block_spending(db_path: str) -> None:
    with _lock:
        _blocked.add(db_path)


def _blocked_path(db_path: str) -> bool:
    with _lock:
        return db_path in _blocked


def paid_dispatch_config(db_path: str) -> SpendConfig:
    config = azure_config()
    if _blocked_path(db_path):
        raise ProviderAdmissionError(UNAVAILABLE)
    with connect(db_path) as conn:
        return _anchored(config, active_allowance(conn))


@dataclass
class _Permit:
    model: str
    input_bound: int
    output_bound: int
    cutoff: float
    used: bool = field(default=False)


_permit: ContextVar[_Permit | None] = ContextVar("azure_dispatch_permit", default=None)


@contextmanager
def azure_dispatch_permit(receipt: ProviderReservation | None) -> Iterator[None]:
    money = receipt.money if receipt is not None else None
    permit = (
        _Permit(money.model, money.input_bound, money.output_bound, money.cutoff)
        if money is not None
        else None
    )
    token = _permit.set(permit)
    try:
        yield
    finally:
        _permit.reset(token)


def claim_azure_dispatch(model: str, input_bound: int, output_bound: int) -> None:
    # The native client sends only under a stored, unused reservation that
    # covers this exact request, so no SDK replay or direct call is unfunded.
    with _lock:
        permit = _permit.get()
        if (
            permit is None
            or permit.used
            or permit.model != model
            or input_bound > permit.input_bound
            or output_bound > permit.output_bound
            or current_time() >= permit.cutoff
        ):
            raise ProviderAdmissionError(UNAVAILABLE)
        permit.used = True


def prepare_spend(request: dict[str, Any], tokens: int, db_path: str) -> SpendReservation | None:
    require_enabled()
    model = str(request.get("model", "")).replace("azure/responses/", "azure/")
    if not model.startswith("azure/"):
        return None
    config = paid_dispatch_config(db_path)
    rates = {**_model_rates(model), "fx": str(config.fx)}
    output = request.get("max_completion_tokens", request.get("max_tokens"))
    if not isinstance(output, int) or isinstance(output, bool) or output <= 0:
        raise ProviderAdmissionError(UNAVAILABLE)
    inputs = tokens - output
    amount = price_cost(rates, inputs, output, cached=0, written=4 * inputs)
    return SpendReservation(
        model,
        current_call_policy().role,
        amount,
        config.total,
        config.cutoff,
        inputs,
        output,
        json.dumps(rates),
        _run_id.get(),
    )


def price_cost(
    rates: dict[str, str], inputs: int, outputs: int, *, cached: int, written: int
) -> int:
    # Rates are USD per million tokens; multiplying by FX yields micro-EUR
    # per token. Reasoning is already within output, never an extra charge.
    tier = "short_" if inputs <= SHORT_CONTEXT_TOKENS and "short_input" in rates else ""
    value = (
        (inputs - cached) * Decimal(rates[f"{tier}input"])
        + cached * Decimal(rates[f"{tier}cached"])
        + outputs * Decimal(rates[f"{tier}output"])
        + written * Decimal(rates[f"{tier}write"])
    ) * Decimal(rates["fx"])
    return _microeur(value)


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _number(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def settled_cost(
    row: SpendRecord, usage: Any
) -> tuple[int, int, int, int | None, int | None] | None:
    if row.settled:
        return None
    prompt = _number(_field(usage, "prompt_tokens"))
    output = _number(_field(usage, "completion_tokens"))
    details = _field(usage, "prompt_tokens_details")
    cached = _number(_field(details, "cached_tokens"))
    written = _number(_field(details, "cache_write_tokens"))
    reasoning = _number(_field(_field(usage, "completion_tokens_details"), "reasoning_tokens"))
    if prompt is None or output is None:
        return None
    rates = json.loads(row.rates)
    if Decimal(rates["write"]) and written is None:
        return None
    if (
        prompt > row.input_bound
        or output > row.output_bound
        or (cached is not None and cached > prompt)
        or (reasoning is not None and reasoning > output)
        or (written is not None and written > 4 * row.input_bound)
    ):
        raise ProviderAdmissionError(UNAVAILABLE)
    return (
        price_cost(rates, prompt, output, cached=cached or 0, written=written or 0),
        prompt,
        output,
        cached,
        written,
    )
