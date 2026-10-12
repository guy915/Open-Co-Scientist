from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
import tiktoken

from co_scientist.core.constants import (
    MINIMAL_REASONING_MAX_TOKENS,
    THINKING_FLOOR_MAX_TOKENS,
)
from co_scientist.core.exceptions import FreeModelEligibilityError
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    ToolLoop,
    call_llm,
    call_llm_json,
    call_llm_with_tools,
    current_run_call_count,
    enforce_free_request,
    scoped_api_key,
    scoped_llm_call_budget,
    scoped_zero_cost_admission,
)
from co_scientist.platform.llm.admission import free_policy as free_catalog
from tests._llm_fake import (
    SEARCH_TOOL,
    _catalog,
    _mock_catalog,
    install_fake_backend,
    make_completion,
    make_message,
    make_tool_call,
    patch_acompletion,
)
from tests._llm_fake import _free_catalog as _isolated_catalog

__all__ = ["_isolated_catalog"]

_MODEL = "openrouter/free/zero:free"
_ZERO_CAP = {"prompt": 0, "completion": 0, "request": 0}
_ZERO = {"prompt": "0", "completion": "0"}


def _spec(**overrides: Any) -> CompletionSpec:
    return CompletionSpec(_MODEL, **overrides)


async def _probe(**overrides: Any) -> str:
    return await call_llm("probe", _spec(**overrides))


@pytest.mark.usefixtures("_isolated_catalog")
class TestFreeAdmission:
    @pytest.mark.parametrize(
        ("pricing", "expiration"),
        [
            (None, None),
            ({}, None),
            ({"prompt": "0", "completion": "0.01"}, None),
            (_ZERO, "not-a-date"),
            (_ZERO, "2000-01-01"),
            *[
                ({"prompt": "0", "completion": price}, None)
                for price in ("NaN", "Infinity", "-1", "1e-999", None, 0, True)
            ],
            *[
                ({**_ZERO, **extra}, None)
                for extra in (
                    {"request": "0.1"},
                    {"input_cache_read": "0.01"},
                    {"internal_reasoning": "0.1"},
                    {"web_search": "0.01"},
                    {"overrides": [{"min_prompt_tokens": 100, "completion": "1"}]},
                )
            ],
        ],
    )
    async def test_an_unverifiable_route_never_reaches_the_provider(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pricing: Any,
        expiration: str | None,
    ) -> None:
        catalog = _catalog(pricing)
        if expiration is not None:
            catalog["data"][0]["expiration_date"] = expiration
        _mock_catalog(monkeypatch, catalog)
        requests: list[dict[str, Any]] = []
        patch_acompletion(monkeypatch, [], requests)
        run_id = str(uuid4())

        with (
            scoped_llm_call_budget(run_id, 10),
            pytest.raises(RuntimeError, match="zero-cost"),
        ):
            await _probe()

        assert requests == []
        assert current_run_call_count(run_id) == 0

    async def test_a_retry_rechecks_prices_before_counting_or_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(free_catalog, "CATALOG_TTL_SECONDS", 0)
        catalog_reads: list[str] = []

        def get(url: str, **kwargs: Any) -> httpx.Response:
            catalog_reads.append(url)
            price = "0" if len(catalog_reads) == 1 else "0.1"
            return httpx.Response(
                200,
                json=_catalog({"prompt": "0", "completion": price}),
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx, "get", get)
        requests: list[dict[str, Any]] = []
        patch_acompletion(
            monkeypatch,
            [make_completion(make_message(""), finish_reason="length")],
            requests,
        )
        run_id = str(uuid4())
        with (
            scoped_llm_call_budget(run_id, 10),
            pytest.raises(RuntimeError, match="zero-cost"),
        ):
            await call_llm_json("probe", _spec())
        assert len(requests) == 1
        assert len(catalog_reads) == 2
        assert current_run_call_count(run_id) == 1

    async def test_a_qualified_route_sends_zero_caps_and_reads_the_catalog_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = _catalog(_ZERO)
        catalog["data"][0]["expiration_date"] = "9999-12-31"
        reads = _mock_catalog(monkeypatch, catalog)
        requests: list[dict[str, Any]] = []
        patch_acompletion(monkeypatch, [make_completion(make_message("ok"))] * 2, requests)

        await _probe()
        await _probe()

        assert len(reads) == 1
        assert len(requests) == 2
        assert requests[0]["extra_body"]["provider"]["max_price"] == _ZERO_CAP
        assert requests[0]["api_base"] == "https://openrouter.ai/api/v1"

    async def test_zero_cost_admission_refuses_a_route_without_free_proof(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_catalog(monkeypatch, _catalog(_ZERO))
        requests: list[dict[str, Any]] = []
        patch_acompletion(monkeypatch, [make_completion(make_message("ok"))], requests)
        paid = CompletionSpec("openrouter/paid/model")

        with (
            scoped_zero_cost_admission(True),
            pytest.raises(RuntimeError, match="zero-cost"),
        ):
            await call_llm("probe", paid)
        assert requests == []

        assert await call_llm("probe", paid) == "ok"
        assert "max_price" not in str(requests[0].get("extra_body"))

    @pytest.mark.parametrize("free_only", [False, True])
    @pytest.mark.parametrize("scoped", [False, True])
    async def test_a_byok_key_skips_the_free_gate_unless_free_models_are_required(
        self, monkeypatch: pytest.MonkeyPatch, free_only: bool, scoped: bool
    ) -> None:
        monkeypatch.setenv("COSCIENTIST_REQUIRE_FREE_MODELS", str(int(free_only)))
        catalog = _catalog({"prompt": "0", "completion": "1"})
        catalog["data"][0]["expiration_date"] = "not-a-date"
        _mock_catalog(monkeypatch, catalog)
        requests: list[dict[str, Any]] = []
        patch_acompletion(monkeypatch, [make_completion(make_message("ok"))], requests)

        with scoped_api_key("byok-test-key" if scoped else None):
            byok = _probe(api_key=None if scoped else "byok-test-key")
            if free_only:
                with pytest.raises(RuntimeError, match="zero-cost"):
                    await byok
            else:
                assert await byok == "ok"

        assert len(requests) == int(not free_only)
        with pytest.raises(RuntimeError, match="zero-cost"):
            await _probe()

    @pytest.mark.parametrize(
        "body",
        [
            {"plugins": [{"id": "web"}]},
            {"model": "paid/model"},
            {"models": ["paid/model"]},
            {"models": ["missing/model:free"]},
            {"models": ["openrouter/free"]},
            {"service_tier": "priority"},
        ],
    )
    async def test_fallbacks_and_addons_outside_the_free_pool_are_rejected(
        self, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
    ) -> None:
        _mock_catalog(monkeypatch, _catalog(_ZERO))
        args = {
            "model": _MODEL,
            "messages": [{"role": "user", "content": "probe"}],
            "extra_body": body,
        }
        with pytest.raises(RuntimeError, match="zero-cost"):
            await enforce_free_request(args)

    @pytest.mark.parametrize(
        "changes",
        [
            {"api_base": "https://different.example/api"},
            {"plugins": []},
            {"tools": [{"type": "web_search"}]},
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": "x"}],
                    }
                ]
            },
            {"model": "openai/paid"},
            {"model": "openrouter/missing/model"},
        ],
    )
    async def test_free_only_mode_rejects_unverified_request_shapes(
        self, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
    ) -> None:
        monkeypatch.setenv("COSCIENTIST_REQUIRE_FREE_MODELS", "1")
        _mock_catalog(monkeypatch, _catalog(_ZERO))
        args = {
            "model": _MODEL,
            "messages": [{"role": "user", "content": "probe"}],
            **changes,
        }
        with pytest.raises(RuntimeError, match="zero-cost"):
            await enforce_free_request(args)

    async def test_an_unreadable_catalog_after_expiry_never_reuses_stale_prices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(free_catalog, "CATALOG_TTL_SECONDS", 0)
        _mock_catalog(monkeypatch, _catalog(_ZERO))
        requests: list[dict[str, Any]] = []
        patch_acompletion(monkeypatch, [make_completion(make_message("ok"))], requests)
        await _probe()

        def unavailable(*args: Any, **kwargs: Any) -> None:
            raise httpx.ConnectError("unavailable")

        monkeypatch.setattr(httpx, "get", unavailable)
        with pytest.raises(RuntimeError, match="zero-cost catalog"):
            await _probe()
        assert len(requests) == 1

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"data": []},
            {"data": None},
            {"data": [{"id": "x"}, {"id": "x"}]},
        ],
    )
    async def test_an_invalid_catalog_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, payload: Any
    ) -> None:
        _mock_catalog(monkeypatch, payload)
        patch_acompletion(monkeypatch, [])
        with pytest.raises(RuntimeError, match="zero-cost catalog"):
            await _probe()

    async def test_an_unknown_promotion_needs_explicit_ancillary_prices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COSCIENTIST_REQUIRE_FREE_MODELS", "1")
        data = _catalog(_ZERO)
        data["data"][0]["id"] = "free/promo"
        _mock_catalog(monkeypatch, data)
        args: dict[str, Any] = {
            "model": "openrouter/free/promo",
            "messages": [{"role": "user", "content": "probe"}],
        }
        with pytest.raises(RuntimeError, match="zero-cost pricing is incomplete"):
            await enforce_free_request(args)
        data["data"][0]["pricing"].update(
            dict.fromkeys(
                (
                    "request",
                    "internal_reasoning",
                    "input_cache_read",
                    "input_cache_write",
                ),
                "0",
            )
        )
        free_catalog._reader.invalidate()
        await enforce_free_request(args)
        assert args["extra_body"]["provider"]["max_price"]["request"] == 0

    def test_the_catalog_cache_is_shared_across_worker_event_loops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = _catalog(_ZERO)
        catalog["data"][0]["expiration_date"] = None
        reads = _mock_catalog(monkeypatch, catalog)

        def verify(_: int) -> None:
            asyncio.run(enforce_free_request({"model": _MODEL, "messages": []}))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(verify, range(8)))
        assert len(reads) == 1

    @pytest.mark.parametrize(
        ("setting", "value"),
        [
            ("model_fallbacks", ["openai/paid"]),
            ("model_alias_map", {_MODEL: "openai/paid"}),
        ],
    )
    async def test_sdk_global_routing_cannot_bypass_admission(
        self, monkeypatch: pytest.MonkeyPatch, setting: str, value: Any
    ) -> None:
        _mock_catalog(monkeypatch, _catalog(_ZERO))
        monkeypatch.setattr(f"litellm.{setting}", value)
        requests: list[dict[str, Any]] = []
        patch_acompletion(monkeypatch, [make_completion(make_message("ok"))], requests)
        with pytest.raises(RuntimeError, match="zero-cost"):
            await _probe()
        assert requests == []

    @pytest.mark.parametrize(
        "changes",
        [
            {"messages": None},
            {"messages": [None]},
            {"messages": "text"},
            {"tools": None},
            {"tools": [None]},
            {"extra_body": {"provider": None}},
        ],
    )
    async def test_malformed_request_containers_raise_a_terminal_policy_error(
        self, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
    ) -> None:
        _mock_catalog(monkeypatch, _catalog(_ZERO))
        with pytest.raises(FreeModelEligibilityError):
            await enforce_free_request({"model": _MODEL, **changes})

    @pytest.mark.parametrize("inputs", ["text", {"text": True}, 1])
    async def test_malformed_input_modalities_are_not_admitted(
        self, monkeypatch: pytest.MonkeyPatch, inputs: Any
    ) -> None:
        data = _catalog(_ZERO)
        data["data"][0]["architecture"]["input_modalities"] = inputs
        _mock_catalog(monkeypatch, data)
        patch_acompletion(monkeypatch, [make_completion(make_message("ok"))])
        with pytest.raises(FreeModelEligibilityError):
            await _probe()

    @pytest.mark.parametrize("thinking", [False, True])
    async def test_nex_requests_fund_and_control_observed_reasoning(
        self, monkeypatch: pytest.MonkeyPatch, thinking: bool
    ) -> None:
        model = "openrouter/nex-agi/nex-n2.5-pro:free"
        catalog = _catalog(_ZERO)
        catalog["data"][0]["id"] = model.removeprefix("openrouter/")
        _mock_catalog(monkeypatch, catalog)
        requests: list[dict[str, Any]] = []
        patch_acompletion(
            monkeypatch,
            [make_completion(make_message('{"answer":"public"}'))],
            requests,
        )
        await call_llm_json(
            "public probe",
            CompletionSpec(
                model_name=model,
                max_tokens=100,
                json_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            ),
            options=LLMCallOptions(enable_thinking=thinking),
            max_attempts=1,
        )
        assert requests[0]["response_format"] == {"type": "json_object"}
        body = requests[0]["extra_body"]
        assert body["provider"]["max_price"] == _ZERO_CAP
        assert "models" not in body
        assert body["reasoning"] == (
            {"enabled": True, "effort": "max"}
            if thinking
            else {"enabled": True, "max_tokens": MINIMAL_REASONING_MAX_TOKENS}
        )
        assert requests[0]["max_tokens"] >= THINKING_FLOOR_MAX_TOKENS


_FREE_MODEL = "openrouter/minimax/minimax-m3:free"


async def _tool_executor(call: Any) -> dict[str, str]:
    return {"role": "tool", "tool_call_id": call.id, "content": "tool result"}


async def _invoke(entry_point: str, spec: CompletionSpec) -> None:
    if entry_point == "text":
        await call_llm("Price ceiling probe", spec)
    elif entry_point == "json_retry":
        await call_llm_json("Price ceiling probe", spec, max_attempts=2)
    else:
        await call_llm_with_tools(
            "Price ceiling probe",
            spec,
            ToolLoop(tools=SEARCH_TOOL, executor=_tool_executor, max_iterations=2),
        )


def _responses(entry_point: str) -> list[SimpleNamespace]:
    final = make_completion(make_message('{"answer": "ok"}'))
    if entry_point == "tools":
        return [
            make_completion(
                make_message(
                    None,
                    tool_calls=[make_tool_call("call-1", "search", '{"q": 1}')],
                )
            ),
            final,
        ]
    if entry_point == "json_retry":
        return [
            make_completion(make_message(""), finish_reason="length"),
            final,
        ]
    return [final]


@pytest.mark.parametrize("entry_point", ["text", "json_retry", "tools"])
@pytest.mark.parametrize("api_key", [None, "test-free-byok-key"])
async def test_free_completions_send_zero_price_ceiling_on_every_attempt(
    monkeypatch: pytest.MonkeyPatch, entry_point: str, api_key: str | None
) -> None:
    requests: list[dict[str, Any]] = []

    patch_acompletion(monkeypatch, _responses(entry_point), recorder=requests)
    await _invoke(entry_point, CompletionSpec(_FREE_MODEL, api_key=api_key))

    assert len(requests) == (1 if entry_point == "text" else 2)
    if entry_point == "tools":
        assert {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "tool result",
        } in requests[1]["messages"]
    if entry_point == "json_retry":
        assert requests[1]["max_tokens"] > requests[0]["max_tokens"]
    for request in requests:
        assert request.get("api_key") == api_key
        body = request["extra_body"]
        assert body["provider"]["max_price"] == _ZERO_CAP
        assert body["models"] == [
            "nvidia/nemotron-3-super-120b-a12b:free",
            "google/gemma-4-31b-it:free",
            "minimax/minimax-m2.7:free",
        ]


async def test_explicit_paid_byok_keeps_its_priced_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        return make_completion(make_message("ok"))

    install_fake_backend(monkeypatch, completion)
    await call_llm(
        "BYOK probe",
        CompletionSpec(model_name="openrouter/z-ai/glm-5.3-flash", api_key="test-byok-key"),
    )

    assert requests[0]["api_key"] == "test-byok-key"
    cap = requests[0]["extra_body"]["provider"]["max_price"]
    assert cap["prompt"] > 0
    assert cap["completion"] > 0


async def test_litellm_serializes_zero_ceiling_into_openrouter_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    # A byte tokenizer prevents an empty local BPE cache from fetching before
    # the mocked transport.
    tokenizer = tiktoken.Encoding(
        name="test-byte-tokenizer",
        pat_str="(?s).",
        mergeable_ranks={bytes([value]): value for value in range(256)},
        special_tokens={},
    )
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _name: tokenizer)

    async def send(
        _client: httpx.AsyncClient, request: httpx.Request, **_kwargs: Any
    ) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "test-zero-cap",
                "object": "chat.completion",
                "created": 1,
                "model": "minimax/minimax-m3:free",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-house-key")
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://unverified.example/v1")
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    answer = await call_llm("Price ceiling probe", CompletionSpec(_FREE_MODEL))

    assert answer == "ok"
    assert len(requests) == 1
    assert str(requests[0].url) == ("https://openrouter.ai/api/v1/chat/completions")
    body = json.loads(requests[0].content)
    assert body["provider"]["max_price"] == _ZERO_CAP


async def test_each_model_is_billed_to_the_key_scoped_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        return make_completion(make_message("ok"))

    install_fake_backend(monkeypatch, completion)
    keys = {"gemini/pro": "supervisor-key"}
    with scoped_api_key("worker-key", by_model=keys):
        for spec in (
            CompletionSpec("openai/worker"),
            CompletionSpec("gemini/pro"),
            CompletionSpec("gemini/pro", api_key="explicit-key"),
        ):
            await call_llm("probe", spec)

    assert [r["api_key"] for r in requests] == [
        "worker-key",
        "supervisor-key",
        "explicit-key",
    ]
