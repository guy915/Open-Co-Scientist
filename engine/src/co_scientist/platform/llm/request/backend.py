"""Worker threads share the process-global backend, without asyncio primitives.
Credentials remain task-local.
"""

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

import litellm
import openai
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from co_scientist.core.exceptions import LLMTimeoutError, ProviderAdmissionError
from co_scientist.platform.llm.profile import model_profile

litellm.suppress_debug_info = True


class _TransportReplayRefusedError(Exception):
    pass


_gateway_request: ContextVar[bool] = ContextVar("gateway_request", default=False)
_sdk_connection_replay = AsyncHTTPHandler.single_connection_post_request


async def _refuse_transport_replay(self: AsyncHTTPHandler, *_args: Any, **_kwargs: Any) -> Any:
    if _gateway_request.get():
        raise _TransportReplayRefusedError(
            "Provider transport failed; outcome unknown; replay blocked"
        )
    return await _sdk_connection_replay(self, *_args, **_kwargs)


# LiteLLM's pinned transport retries disconnects independently of both retry
# settings. This helper is used only for those replays, including stream setup.
AsyncHTTPHandler.single_connection_post_request = _refuse_transport_replay  # type: ignore[method-assign]


def _is_transport_replay(error: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, _TransportReplayRefusedError):
            return True
        pending.extend(
            cause for cause in (current.__cause__, current.__context__) if cause is not None
        )
    return False


class CompletionBackend(Protocol):
    """The answering backend also determines the native response format."""

    async def complete(self, **completion_args: Any) -> Any: ...

    def supports_json_schema(self, model_name: str) -> bool: ...


def is_azure_model(model: str) -> bool:
    return model.lower().startswith(("azure/", "azure_ai/", "azure_text/"))


def is_authentication_error(error: BaseException) -> bool:
    return isinstance(error, (litellm.exceptions.AuthenticationError, openai.AuthenticationError))


def litellm_supports_json_schema(model_name: str) -> bool:
    """Model capability is process-static; explicit profiles override
    unreliable registry answers.
    """
    stated = model_profile(model_name).json_schema
    if stated is not None:
        return stated
    return _registry_supports_json_schema(model_name)


@functools.cache
def _registry_supports_json_schema(model_name: str) -> bool:
    try:
        return bool(litellm.supports_response_schema(model=model_name))
    except Exception:
        return True


class LitellmBackend:
    operator_routing = True

    async def complete(self, **completion_args: Any) -> Any:
        """Read the live LiteLLM attribute so existing patch paths still
        steer built requests.
        """
        model = str(completion_args.get("model", "")).replace("azure/responses/", "azure/")
        from co_scientist.platform.llm.request.azure import LUNA, AzureResponsesBackend

        if model == LUNA:
            backend = AzureResponsesBackend.from_environment()
            try:
                response = await backend.complete(**completion_args)
            except BaseException:
                backend.close()
                raise
            if completion_args.get("stream"):
                return _OwnedStream(response, backend.close)
            backend.close()
            return response
        if is_azure_model(model):
            # LiteLLM would read the operator's Azure key from the environment
            # without the native client's funding permit.
            raise ProviderAdmissionError("No model is available right now")
        # SDK retries would send requests that admission has not reserved.
        completion_args.update(num_retries=0, max_retries=0)
        token = _gateway_request.set(True)
        try:
            return await litellm.acompletion(**completion_args)
        except Exception as error:
            if _is_transport_replay(error):
                raise LLMTimeoutError(
                    "Provider transport failed; outcome unknown; replay blocked"
                ) from error
            raise
        finally:
            _gateway_request.reset(token)

    def supports_json_schema(self, model_name: str) -> bool:
        return litellm_supports_json_schema(model_name)


class _OwnedStream:
    def __init__(self, response: Any, close_client: Callable[[], None]) -> None:
        self._response = response
        self._iterator = response.__aiter__()
        self._close_client = close_client
        self._closed = False

    def __aiter__(self) -> "_OwnedStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._iterator.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                await self._response.aclose()
            finally:
                self._close_client()


_LITELLM = LitellmBackend()

# None distinguishes no installed backend from an installed default-like
# backend.
_installed: CompletionBackend | None = None


def active_backend() -> CompletionBackend:
    return _LITELLM if _installed is None else _installed


def install_backend(
    backend: CompletionBackend | None,
) -> CompletionBackend | None:
    global _installed
    previous, _installed = _installed, backend
    return previous


@contextmanager
def using_backend(backend: CompletionBackend) -> Iterator[CompletionBackend]:
    previous = install_backend(backend)
    try:
        yield backend
    finally:
        install_backend(previous)
