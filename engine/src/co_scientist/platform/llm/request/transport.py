"""Streaming consumers own silence and total deadlines; this layer bounds
establishment.
"""

import asyncio
import time
from collections.abc import Callable
from contextvars import copy_context
from typing import Any

from litellm.exceptions import Timeout as LiteLLMTimeout
from openai import APIStatusError
from opentelemetry.trace import Span

from co_scientist.core import inflight
from co_scientist.core.exceptions import (
    LLMTimeoutError,
    ProviderAdmissionError,
    ProviderCreditExhaustedError,
)
from co_scientist.platform.db.admission import ProviderReservation
from co_scientist.platform.llm.admission.anthropic import (
    mark_credit_exhausted,
    require_credit_available,
)
from co_scientist.platform.llm.admission.call_budget import record_provider_request
from co_scientist.platform.llm.admission.free_policy import enforce_free_request
from co_scientist.platform.llm.admission.service import (
    current_db_path,
    reserve_physical,
    settle_physical,
)
from co_scientist.platform.llm.admission.spend import (
    azure_dispatch_permit,
    paid_dispatch_config,
    require_enabled,
)
from co_scientist.platform.llm.profile import model_profile
from co_scientist.platform.llm.request.anthropic import (
    API_BASE,
    AnthropicSlotUnavailableError,
    is_credit_error,
    require_prompt_fits,
)
from co_scientist.platform.llm.request.azure import LUNA, request_input_bound
from co_scientist.platform.llm.request.backend import active_backend, is_azure_model
from co_scientist.platform.llm.request.cache import (
    HAIKU,
    apply_dispatch_cache_key,
    apply_prompt_cache,
)
from co_scientist.platform.llm.request.response import is_model_refusal
from co_scientist.platform.llm.request.thinking import (
    _apply_thinking_args,
    apply_provider_constraints,
)
from co_scientist.platform.llm.roles import current_call_policy
from co_scientist.platform.llm.routing import routed_completion, routing_scope
from co_scientist.platform.llm.telemetry import (
    end_request_span,
    record_completion_failure,
    record_completion_response,
    start_request_span,
)


class _CompletionStream:
    """Consumption may finish in another context; usage belongs to the
    initiating task.
    """

    def __init__(
        self,
        response: Any,
        model: str,
        start: float,
        span: Span,
        receipt: ProviderReservation | None,
        call: inflight.CallMark,
        byok: bool,
    ) -> None:
        self._response = response
        self._call = call
        self._byok = byok
        self._iterator = response.__aiter__()
        self._model = model
        self._start = start
        self._span = span
        self._context = copy_context()
        self._last = response
        self._done = False
        self._receipt = receipt
        self._refused = False

    def __aiter__(self) -> "_CompletionStream":
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self._iterator.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as error:
            self._finish(error)
            if self._receipt is not None and self._receipt.credit and is_credit_error(error):
                self._call.answered()
                mark_credit_exhausted(self._receipt.db_path)
                raise AnthropicSlotUnavailableError("No model is available right now") from error
            if self._byok and is_credit_error(error):
                self._call.answered()
                raise ProviderCreditExhaustedError(
                    "Provider API credits exhausted. Top up your provider account, "
                    "then resume this run."
                ) from error
            raise
        self._refused = self._refused or is_model_refusal(chunk)
        if getattr(chunk, "usage", None) is not None:
            self._last = chunk
        return chunk

    def _finish(self, error: BaseException | None = None) -> None:
        if self._done:
            return
        self._done = True
        latency = time.monotonic() - self._start
        _note_answer(self._call, error)
        if error is None:
            try:
                settle_physical(self._receipt, self._last, refused=self._refused)
            except BaseException as settlement_error:
                self._context.run(
                    record_completion_failure,
                    self._model,
                    settlement_error,
                    latency,
                    refused=self._refused,
                )
                end_request_span(self._span, self._last, settlement_error, refused=self._refused)
                raise
            self._context.run(
                record_completion_response, self._model, self._last, latency, refused=self._refused
            )
        else:
            self._context.run(
                record_completion_failure, self._model, error, latency, refused=self._refused
            )
        end_request_span(self._span, self._last, error, refused=self._refused)

    async def aclose(self) -> None:
        self._finish(asyncio.CancelledError())
        close = getattr(self._response, "aclose", None)
        if close is not None:
            await close()


# Only an HTTP status the provider returned proves the request was answered;
# connection loss, timeouts and cancellation leave its outcome unknown.
def _note_answer(call: inflight.CallMark, error: BaseException | None) -> None:
    if error is None or isinstance(error, APIStatusError):
        call.answered()


async def _await_provider(
    args: dict[str, Any], model: str, timeout: float | None, grace: float
) -> Any:
    backend = active_backend()
    if timeout is None:
        return await backend.complete(**args)
    try:
        return await asyncio.wait_for(backend.complete(**args), timeout=timeout + grace)
    except asyncio.TimeoutError as exc:
        raise LLMTimeoutError(
            f"LLM call to {model} exceeded {timeout}s without a response"
        ) from exc


def _routed_args(original: dict[str, Any], model: str) -> dict[str, Any]:
    from co_scientist.core.config import settings

    args = dict(original)
    output = args.pop(
        "max_completion_tokens", args.get("max_tokens", settings.app_llm_max_output_tokens)
    )
    for key in (
        "extra_body",
        "api_key",
        "api_base",
        "thinking",
        "output_config",
        "allowed_openai_params",
        "reasoning_effort",
        "cache_control",
        "prompt_cache_key",
    ):
        args.pop(key, None)
    args["model"] = model
    args["max_tokens"] = output
    args["messages"] = [dict(message) for message in original.get("messages", ())]
    fmt = args.get("response_format") or {}
    if fmt.get("type") == "json_schema" and not model_profile(model).json_schema:
        from co_scientist.platform.llm.request.completion import _inject_schema_into_prompt

        for message in reversed(args["messages"]):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                message["content"] = _inject_schema_into_prompt(
                    message["content"], fmt["json_schema"]
                )
                break
        args.pop("response_format", None)
    elif fmt and fmt.get("type") != "json_schema" and not model_profile(model).json_object:
        args.pop("response_format", None)
    policy = current_call_policy()
    _apply_thinking_args(args, model, policy.enable_thinking)
    long_roles = {"overview", "meta_review", "literature_analysis"}
    ceiling = 600.0 if policy.role in long_roles else 180.0
    requested_timeout = args.get("timeout")
    args["timeout"] = (
        min(ceiling, float(requested_timeout)) if requested_timeout is not None else ceiling
    )
    return args


async def complete_request(
    completion_args: dict[str, Any],
    model_name: str,
    *,
    byok: bool,
    timeout_seconds: float | None,
    timeout_grace_seconds: float = 0.0,
    before_dispatch: Callable[[], None] | None = None,
) -> Any:
    async def dispatch(args: dict[str, Any], model: str) -> Any:
        requested = args.get("timeout", timeout_seconds)
        timeout = float(requested) if requested is not None else None
        return await _complete_physical(
            args,
            model,
            byok=byok,
            timeout_seconds=timeout,
            timeout_grace_seconds=timeout_grace_seconds,
            before_dispatch=before_dispatch,
        )

    if (
        byok
        or routing_scope() is None
        or model_name.startswith("offline/")
        or not getattr(active_backend(), "operator_routing", False)
    ):
        return await dispatch(completion_args, model_name)
    return await routed_completion(
        completion_args,
        model_name,
        dispatch=dispatch,
        prepare=_routed_args,
        refused=is_model_refusal,
    )


async def _input_tokens_bound(
    completion_args: dict[str, Any], model_name: str, *, byok: bool, azure: bool
) -> int | None:
    from co_scientist.core.config import settings

    if azure:
        if "max_completion_tokens" not in completion_args:
            completion_args.setdefault("max_tokens", settings.app_llm_max_output_tokens)
        # Validates the request shape and bounds what the provider will bill.
        return request_input_bound(completion_args)
    if byok or model_name != HAIKU:
        return None
    try:
        bound = await require_prompt_fits(completion_args, current_db_path())
    except BaseException as preflight_error:
        if is_credit_error(preflight_error):
            mark_credit_exhausted(current_db_path())
            raise AnthropicSlotUnavailableError(
                "No model is available right now"
            ) from preflight_error
        raise
    completion_args["api_base"] = API_BASE
    return bound


async def _complete_physical(
    completion_args: dict[str, Any],
    model_name: str,
    *,
    byok: bool,
    timeout_seconds: float | None,
    timeout_grace_seconds: float = 0.0,
    before_dispatch: Callable[[], None] | None = None,
) -> Any:
    """No retry occurs at this physical-call seam; stream clocks remain
    caller-owned.
    """
    from co_scientist.core.config import settings

    require_enabled()
    native_model = str(completion_args.get("model", "")).replace("azure/responses/", "azure/")
    azure = is_azure_model(native_model) or is_azure_model(model_name)
    if azure and (byok or native_model != LUNA):
        # The native deployment client owns its key; a caller key cannot
        # establish caller funding, and other Azure routes have no permit.
        raise ProviderAdmissionError("No model is available right now")
    apply_provider_constraints(completion_args, model_name)
    apply_prompt_cache(completion_args, model_name)
    zero_cost = await enforce_free_request(completion_args, byok=byok)
    input_tokens_bound = await _input_tokens_bound(
        completion_args, model_name, byok=byok, azure=azure
    )
    record_provider_request()
    receipt = None
    if not byok:
        # Bound requests that previously delegated an unbounded output default
        # to the SDK. Admission covers every scientific and app retry here.
        if "max_completion_tokens" not in completion_args:
            completion_args.setdefault("max_tokens", settings.app_llm_max_output_tokens)
        receipt = reserve_physical(completion_args, input_tokens_bound=input_tokens_bound)
    if before_dispatch is not None:
        before_dispatch()
    require_enabled()
    if receipt is not None and receipt.paid:
        paid_dispatch_config(receipt.db_path)
    if receipt is not None and receipt.credit:
        require_credit_available(receipt.db_path, dispatch=True)
    apply_dispatch_cache_key(completion_args, model_name)
    span = start_request_span(model_name, completion_args)
    start = time.monotonic()
    call = await inflight.begin_provider_call()
    try:
        with azure_dispatch_permit(receipt):
            response = await _await_provider(
                completion_args, model_name, timeout_seconds, timeout_grace_seconds
            )
    except (LLMTimeoutError, LiteLLMTimeout) as exc:
        error = LLMTimeoutError(
            f"LLM call to {model_name} timed out without a response; "
            "provider outcome may be unknown",
            zero_cost_admitted=zero_cost and not byok,
        )
        record_completion_failure(model_name, error, time.monotonic() - start)
        end_request_span(span, None, error)
        raise error from exc
    except BaseException as exc:
        _note_answer(call, exc)
        record_completion_failure(model_name, exc, time.monotonic() - start)
        end_request_span(span, None, exc)
        if receipt is not None and receipt.credit and is_credit_error(exc):
            call.answered()
            mark_credit_exhausted(receipt.db_path)
            raise AnthropicSlotUnavailableError("No model is available right now") from exc
        if byok and is_credit_error(exc):
            call.answered()
            raise ProviderCreditExhaustedError(
                "Provider API credits exhausted. Top up your provider account, "
                "then resume this run."
            ) from exc
        raise
    if completion_args.get("stream"):
        return _CompletionStream(response, model_name, start, span, receipt, call, byok)
    call.answered()
    try:
        settle_physical(receipt, response, refused=is_model_refusal(response))
    except BaseException as settlement_error:
        record_completion_failure(model_name, settlement_error, time.monotonic() - start)
        end_request_span(span, response, settlement_error)
        raise
    record_completion_response(model_name, response, time.monotonic() - start)
    end_request_span(span, response, None)
    return response
