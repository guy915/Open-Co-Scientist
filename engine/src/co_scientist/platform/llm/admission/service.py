from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.platform.db import connect, transaction
from co_scientist.platform.db.admission import (
    UNKNOWN_HOST,
    ProviderReservation,
    reserve_provider,
    settle_provider,
)
from co_scientist.platform.db.anthropic_credit import credit_record, disable_credit
from co_scientist.platform.db.spend import hold_spending, spend_record
from co_scientist.platform.llm.admission.anthropic import (
    block_credit,
    credit_settlement,
    prepare_credit,
)
from co_scientist.platform.llm.admission.spend import (
    block_spending,
    prepare_spend,
    settled_cost,
)

_app: ContextVar[bool] = ContextVar("service_admission_app", default=False)


@contextmanager
def scoped_app_admission() -> Iterator[None]:
    token = _app.set(True)
    try:
        yield
    finally:
        _app.reset(token)


_client: ContextVar[str] = ContextVar("provider_usage_client", default="")
_host: ContextVar[str] = ContextVar("provider_usage_host", default=UNKNOWN_HOST)
_path: ContextVar[str | None] = ContextVar("provider_usage_path", default=None)
_free_ceiling: ContextVar[int | None] = ContextVar("free_route_ceiling", default=None)


@contextmanager
def scoped_free_route(ceiling: int | None) -> Iterator[None]:
    token = _free_ceiling.set(ceiling)
    try:
        yield
    finally:
        _free_ceiling.reset(token)


def current_db_path() -> str:
    from co_scientist.platform.db import default_db_path

    return _path.get() or default_db_path() or "./coscientist.db"


@contextmanager
def scoped_client(
    owner: str, *, host: str = UNKNOWN_HOST, db_path: str | None = None
) -> Iterator[None]:
    tokens = _client.set(owner), _host.set(host), _path.set(db_path)
    try:
        yield
    finally:
        _client.reset(tokens[0])
        _host.reset(tokens[1])
        _path.reset(tokens[2])


def _token_reservation(
    request: dict[str, Any], *, app: bool, input_tokens_bound: int | None = None
) -> int:
    from co_scientist.core.config import settings

    output = request.get("max_completion_tokens", request.get("max_tokens"))
    output_limit = (
        settings.app_llm_max_output_tokens if app else settings.provider_max_output_tokens
    )
    input_limit = settings.app_llm_max_input_bytes if app else settings.provider_max_input_bytes
    if (
        not isinstance(output, int)
        or isinstance(output, bool)
        or not 0 < output <= output_limit
        or request.get("n", 1) != 1
    ):
        raise ProviderAdmissionError("completion output budget exceeded")
    content = {key: request.get(key) for key in ("messages", "tools", "functions")}
    input_bytes = len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
    if input_bytes > input_limit:
        raise ProviderAdmissionError("completion input budget exceeded")
    # Conservative byte-token bound plus the entire output cap. Failed requests
    # and interrupted streams retain the reservation, including after restart.
    inputs = input_bytes + 1024
    if input_tokens_bound is not None:
        if type(input_tokens_bound) is not int or input_tokens_bound < 0:
            raise ProviderAdmissionError("completion input budget exceeded")
        inputs = max(inputs, input_tokens_bound + 1024)
    return inputs + output


def reserve_physical(
    request: dict[str, Any], *, app: bool | None = None, input_tokens_bound: int | None = None
) -> ProviderReservation:
    app = _app.get() if app is None else app
    tokens = _token_reservation(request, app=app, input_tokens_bound=input_tokens_bound)
    path = current_db_path()
    spend = prepare_spend(request, tokens, path)
    credit = prepare_credit(request, tokens, path)
    return reserve_provider(
        _client.get(),
        _host.get(),
        tokens,
        app=app,
        db_path=path,
        spend=spend,
        credit=credit,
        free_daily_ceiling=_free_ceiling.get(),
    )


def settle_physical(
    receipt: ProviderReservation | None, response: Any, *, refused: bool | None = None
) -> None:
    if receipt is None:
        return
    usage = (
        response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    )
    total = 0
    for name in ("prompt_tokens", "completion_tokens"):
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return
        total += value
    try:
        row = spend_record(receipt.id, receipt.db_path) if receipt.paid else None
        if receipt.paid and row is None:
            raise ProviderAdmissionError("No model is available right now")
        money = settled_cost(row, usage) if row is not None else None
        credit_money = None
        if receipt.credit:
            with connect(receipt.db_path) as conn:
                credit = credit_record(conn, receipt.id)
            if credit is None:
                raise ProviderAdmissionError("No model is available right now")
            details = (
                usage.get("prompt_tokens_details")
                if isinstance(usage, dict)
                else getattr(usage, "prompt_tokens_details", None)
            )

            def field(value: Any, name: str) -> Any:
                return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

            written = field(details, "cache_write_tokens")
            if written is None:
                written = field(details, "cache_creation_tokens")
            credit_money = credit_settlement(
                credit,
                prompt=field(usage, "prompt_tokens"),
                output=field(usage, "completion_tokens"),
                cached=field(details, "cached_tokens"),
                written=written,
            )
        settle_provider(receipt, total, money, credit_money, refused=refused)
    except Exception as error:
        if receipt.credit:
            block_credit(receipt.db_path)
            with contextlib.suppress(Exception), transaction(receipt.db_path, durable=True) as conn:
                disable_credit(conn, float("inf"))
            raise ProviderAdmissionError("No model is available right now") from error
        if receipt.paid:
            block_spending(receipt.db_path)
            # A failed writer may also prevent the durable hold. The process
            # hold stops calls while the original full reservation survives.
            with contextlib.suppress(Exception):
                hold_spending(receipt.id, receipt.db_path)
            raise ProviderAdmissionError("No model is available right now") from error
        raise
