from __future__ import annotations

from typing import Any

import httpx
import openai
import pytest

from co_scientist.core.exceptions import ProviderCreditExhaustedError
from co_scientist.platform.llm.request.anthropic import is_credit_error
from co_scientist.platform.llm.request.transport import complete_request
from tests._llm_fake import install_fake_backend


@pytest.mark.parametrize("status,code", [(402, "payment_required"), (429, "insufficient_quota")])
async def test_rejected_byok_credit_becomes_recoverable_control_flow(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    response = httpx.Response(status, request=httpx.Request("POST", "https://provider.example"))
    error = openai.APIStatusError("Insufficient credits", response=response, body={"code": code})

    async def provider(**kwargs: Any) -> Any:
        raise error

    fake = install_fake_backend(monkeypatch, provider)
    with pytest.raises(ProviderCreditExhaustedError, match="Top up"):
        await complete_request(
            {"model": "gpt-4o-mini", "max_tokens": 10}, "gpt-4o-mini", byok=True, timeout_seconds=1
        )
    assert len(fake.requests) == 1


def test_generated_error_text_cannot_claim_credit_exhaustion() -> None:
    assert not is_credit_error(ValueError("insufficient credits HTTP 402"))
    response = httpx.Response(429, request=httpx.Request("POST", "https://provider.example"))
    assert not is_credit_error(
        openai.APIStatusError("Rate limit reached", response=response, body={})
    )


@pytest.mark.parametrize("byok", [True, False])
async def test_stream_credit_rejection_keeps_caller_and_operator_funding_distinct(
    monkeypatch: pytest.MonkeyPatch, byok: bool
) -> None:
    from co_scientist.platform.llm.request import transport

    response = httpx.Response(402, request=httpx.Request("POST", "https://provider.example"))
    error = httpx.HTTPStatusError("Payment required", request=response.request, response=response)

    async def chunks() -> Any:
        raise error
        yield None

    async def provider(**kwargs: Any) -> Any:
        return chunks()

    async def free_request(args: dict[str, Any], *, byok: bool) -> bool:
        return not byok

    install_fake_backend(monkeypatch, provider)
    monkeypatch.setattr(transport, "enforce_free_request", free_request)
    monkeypatch.setattr(transport, "reserve_physical", lambda *args, **kwargs: None)
    stream = await complete_request(
        {"model": "gpt-4o-mini", "max_tokens": 10, "stream": True},
        "gpt-4o-mini",
        byok=byok,
        timeout_seconds=1,
    )
    expected = ProviderCreditExhaustedError if byok else httpx.HTTPStatusError
    with pytest.raises(expected):
        await stream.__anext__()
