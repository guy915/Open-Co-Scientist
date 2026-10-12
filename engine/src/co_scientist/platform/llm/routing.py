from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from co_scientist.core.exceptions import (
    FreeModelEligibilityError,
    LLMTimeoutError,
    ProviderAdmissionError,
)
from co_scientist.platform.db import Connection, connect
from co_scientist.platform.db.anthropic_credit import (
    AnthropicCreditUnavailableError,
    AnthropicPromptTooLongError,
)
from co_scientist.platform.db.llm_routes import (
    OpenRouterCapacityError,
    Slot,
    exhaust_free_routes,
    free_available,
    reacquire_forecast,
    record_switch,
    route_for_run,
)
from co_scientist.platform.db.spend import UNAVAILABLE
from co_scientist.platform.llm.admission.anthropic import require_credit_available
from co_scientist.platform.llm.admission.free_policy import free_models_required
from co_scientist.platform.llm.admission.service import (
    current_db_path,
    scoped_free_route,
)
from co_scientist.platform.llm.admission.spend import (
    SHORT_CONTEXT_TOKENS,
    SpendConfig,
    paid_dispatch_config,
    require_enabled,
    scoped_run_spending,
)
from co_scientist.platform.llm.profile import HAIKU, is_free_route, model_profile
from co_scientist.platform.llm.roles import current_call_policy

logger = logging.getLogger(__name__)
_LUNA = "azure/gpt-6-luna-2026-09-22"


@dataclass(frozen=True)
class RoutingScope:
    run_id: str | None = None


_scope: ContextVar[RoutingScope | None] = ContextVar("operator_model_routing", default=None)


@contextmanager
def scoped_operator_routing(run_id: str | None = None, *, enabled: bool = True) -> Iterator[None]:
    if not enabled or (run_id is None and _scope.get() is not None):
        yield
        return
    token = _scope.set(RoutingScope(run_id))
    try:
        yield
    finally:
        _scope.reset(token)


def routing_scope() -> RoutingScope | None:
    return _scope.get()


def free_call_ceiling() -> int:
    try:
        value = int(os.getenv("LLM_OPENROUTER_CALLS_PER_DAY", "1000"))
    except ValueError as error:
        raise ProviderAdmissionError(UNAVAILABLE) from error
    if not 0 < value < 2**63:
        raise ProviderAdmissionError(UNAVAILABLE)
    return value


@dataclass(frozen=True)
class RoutingAdmission:
    slots: tuple[Slot, ...]
    azure: SpendConfig | None


@dataclass(frozen=True)
class RunReadmission:
    plan: RoutingAdmission
    estimate: int


def prepare_readmission(
    run_id: str, goal: str, config: dict[str, Any], *, db_path: str | None = None
) -> RunReadmission | None:
    path = db_path or current_db_path()
    if route_for_run(run_id, path) is None:
        return None
    plan = available_slots(path)
    estimate = express_estimate(goal, config, plan.azure) if plan.azure else 0
    return RunReadmission(plan, estimate)


def recheck_readmission(conn: Connection, run_id: str, admission: RunReadmission | None) -> None:
    if admission is None:
        return
    require_enabled()
    reacquire_forecast(
        conn,
        run_id,
        total=admission.plan.azure.total if admission.plan.azure else None,
        estimate=admission.estimate,
    )
    row = conn.execute("SELECT azure_allowed FROM llm_routes WHERE run_id=?", (run_id,)).fetchone()
    if row is None or not any(slot != "azure" or row[0] for slot in admission.plan.slots):
        raise ProviderAdmissionError(UNAVAILABLE)


def available_slots(path: str) -> RoutingAdmission:
    require_enabled()
    slots: list[Slot] = []
    if os.getenv("OPENROUTER_API_KEY") and free_available(path, free_call_ceiling()):
        slots.append("openrouter")
    azure = None
    if not free_models_required():
        if os.getenv("ANTHROPIC_API_KEY"):
            try:
                require_credit_available(path)
                slots.append("anthropic")
            except ProviderAdmissionError:
                pass
        if all(
            os.getenv(name)
            for name in (
                "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_DEPLOYMENT",
            )
        ):
            try:
                azure = paid_dispatch_config(path)
                slots.append("azure")
            except ProviderAdmissionError:
                pass
    return RoutingAdmission(tuple(slots), azure)


def express_estimate(goal: str, config: dict[str, Any], azure: SpendConfig) -> int:
    # The offline call meter's Express envelope is 57 calls / 720k requested output.
    # This is a forecast, not measured usage. Physical reserves remain hard.
    from decimal import ROUND_CEILING, Decimal

    price = model_profile(_LUNA).price
    if price is None:
        raise ProviderAdmissionError(UNAVAILABLE)
    # The offline meter measured four supervisor calls. Allow twelve, plus 45 worker calls,
    # 100k input bytes each, at the Luna tier those bytes fall in.
    input_bound = 100_000 + len(goal.encode("utf-8")) + 1024
    if input_bound > SHORT_CONTEXT_TOKENS:
        price = price.long_context or price
    inputs = (
        57
        * input_bound
        * (
            Decimal(str(price.prompt_usd_per_million))
            + 4 * Decimal(str(price.cache_write_usd_per_million))
        )
    )
    configured_calls = config.get("max_llm_calls", 1200)
    if type(configured_calls) is not int or configured_calls <= 0:
        raise ProviderAdmissionError(UNAVAILABLE)
    factor = max(Decimal(1), Decimal(configured_calls) / 1200)
    estimate = (
        (inputs + 720_000 * Decimal(str(price.completion_usd_per_million))) * factor * azure.fx
    )
    return int(estimate.to_integral_value(rounding=ROUND_CEILING))


class _BufferedStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = iter(chunks)

    def __aiter__(self) -> _BufferedStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self._chunks = iter(())


def _output_limited(response: Any) -> bool:
    choices = (
        response.get("choices", ())
        if isinstance(response, dict)
        else getattr(response, "choices", ())
    )
    return any(
        (
            choice.get("finish_reason")
            if isinstance(choice, dict)
            else getattr(choice, "finish_reason", None)
        )
        in ("length", "max_tokens")
        for choice in choices
    )


async def _buffer(response: Any, refused: Callable[[Any], bool]) -> tuple[Any, bool, bool]:
    chunks: list[Any] = []
    declined = False
    limited = False
    size = 0
    try:
        async for chunk in response:
            declined = declined or refused(chunk)
            limited = limited or _output_limited(chunk)
            size += len(str(chunk))
            if size > 8 * 1024 * 1024 or len(chunks) >= 65536:
                raise LLMTimeoutError(
                    "Provider stream exceeded its bounded buffer; outcome unknown"
                )
            chunks.append(chunk)
    finally:
        close = getattr(response, "aclose", None)
        if close is not None:
            await close()
    return _BufferedStream(chunks), declined, limited


def _provider_quota(error: Exception) -> bool:
    named = type(error).__name__ in {
        "RateLimitError",
        "AuthenticationError",
        "PermissionDeniedError",
    }
    if named:
        return True
    if type(error).__name__ == "HTTPStatusError":
        return getattr(getattr(error, "response", None), "status_code", None) in (
            401,
            402,
            403,
            429,
        )
    return getattr(error, "status_code", None) in (401, 402, 403, 429)


def _model(slot: Slot, original: str) -> str:
    from co_scientist.core.config import DEFAULT_MODEL

    if slot == "openrouter":
        return (
            original
            if original.startswith("openrouter/") and is_free_route(original)
            else DEFAULT_MODEL
        )
    if slot == "anthropic":
        return HAIKU
    return _LUNA


def _choices(scope: RoutingScope, path: str) -> tuple[Slot, ...]:
    choices = available_slots(path).slots
    if scope.run_id is None:
        return choices
    with connect(path) as conn:
        run = conn.execute("SELECT profile FROM runs WHERE id=?", (scope.run_id,)).fetchone()
    if run is None or run[0] != "express":
        raise ProviderAdmissionError(UNAVAILABLE)
    route = route_for_run(scope.run_id, path)
    if route is None:
        raise ProviderAdmissionError(UNAVAILABLE)
    choices = tuple(slot for slot in choices if slot != "azure" or route.azure_allowed)
    # A run keeps its admitted provider until that provider is unavailable.
    if route.slot in choices:
        choices = choices[choices.index(route.slot) :]
    return choices


async def _read_response(
    response: Any, args: dict[str, Any], slot: Slot, refused: Callable[[Any], bool]
) -> tuple[Any, bool, bool]:
    if not args.get("stream") or slot != "anthropic":
        return response, refused(response), _output_limited(response)
    try:
        return await asyncio.wait_for(
            _buffer(response, refused), timeout=float(args.get("timeout", 600))
        )
    except asyncio.TimeoutError as error:
        raise LLMTimeoutError("Provider stream exceeded its deadline; outcome unknown") from error


async def routed_completion(
    request: dict[str, Any],
    original: str,
    *,
    dispatch: Callable[[dict[str, Any], str], Awaitable[Any]],
    prepare: Callable[[dict[str, Any], str], dict[str, Any]],
    refused: Callable[[Any], bool],
) -> Any:
    require_enabled()
    scope = _scope.get()
    if scope is None:
        return await dispatch(request, original)
    path = current_db_path()
    choices = _choices(scope, path)
    previous: Slot | None = None
    reason = "unavailable"
    sticky = True
    if scope.run_id is not None:
        route = route_for_run(scope.run_id, path)
        previous = route.slot if route else None
    for slot in choices:
        if previous is not None and slot != previous:
            if scope.run_id is not None:
                record_switch(
                    path,
                    scope.run_id,
                    previous,
                    slot,
                    reason,
                    current_call_policy().role,
                    sticky=sticky,
                )
            logger.info(
                "Model provider switch from=%s to=%s reason=%s role=%s",
                previous,
                slot,
                reason,
                current_call_policy().role,
            )
        args = prepare(request, _model(slot, original))
        try:
            with (
                scoped_free_route(free_call_ceiling() if slot == "openrouter" else None),
                scoped_run_spending(scope.run_id),
            ):
                response = await dispatch(args, str(args["model"]))
                response, declined, limited = await _read_response(response, args, slot, refused)
            if declined or (limited and slot == "anthropic"):
                previous, reason, sticky = slot, "refusal" if declined else "output_cap", False
                continue
            return response
        except (AnthropicCreditUnavailableError, OpenRouterCapacityError) as error:
            # Luna's long-context tier starts far higher; later calls return to Haiku.
            long = isinstance(error, AnthropicPromptTooLongError)
            previous, reason = slot, "long_context" if long else "credit_or_free_cap"
            sticky = sticky and not long
        except FreeModelEligibilityError:
            if slot != "openrouter":
                raise
            exhaust_free_routes(path)
            previous, reason = slot, "free_route_unavailable"
        except Exception as error:
            if slot == "azure":
                if isinstance(error, (ProviderAdmissionError, LLMTimeoutError)):
                    raise
                raise ProviderAdmissionError(UNAVAILABLE) from error
            if not _provider_quota(error):
                raise
            if slot == "openrouter":
                exhaust_free_routes(path)
            previous, reason = slot, "provider_quota"
    raise ProviderAdmissionError(UNAVAILABLE)
