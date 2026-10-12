"""Input ceilings use the bundled cl100k tokenizer, not provider billing usage."""

from __future__ import annotations

import collections
import dataclasses
import json
import os
import socket
import time
from importlib.resources import files
from typing import Any

import pytest
import tiktoken
from co_scientist.core.config import settings
from co_scientist.core.prompt_cache import CacheablePrompt
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus
from co_scientist.platform.llm.offline import llm as offline_llm
from co_scientist.platform.llm.request import backend
from co_scientist.platform.llm.request.completion import _inject_schema_into_prompt
from co_scientist.platform.retrieval import mcp_client
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from . import test_run_envelopes as meter
from ._envelope_fakes import FakeMCPClient
from ._process_mode_helpers import FakeProcessMode
from ._store_helpers import drive_offline_run, seed_run

PROMPT_TOKEN_CEILING = 8_000


class _PromptMCPClient(FakeMCPClient):
    def __init__(self, connections: Any, *, handle_tool_errors: bool = False) -> None:
        super().__init__(connections)


@dataclasses.dataclass
class _PromptUsage:
    calls: int = 0
    estimated_tokens: int = 0
    max_tokens: int = 0
    cacheable_prefix_tokens: int = 0
    max_cacheable_prefix_tokens: int = 0
    completion_minutes: float = 0


class _PromptRecorder:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.encoding = tiktoken.get_encoding("cl100k_base")
        self.attempts: dict[int, tuple[int, int, float]] = {}

    async def complete(self, **kwargs: Any) -> Any:
        attempt = trace.get_current_span().get_span_context().span_id
        tokens = sum(
            len(self.encoding.encode(str(message.get("content") or ""), disallowed_special=()))
            for message in kwargs.get("messages") or [{}]
        )
        prefix_tokens = sum(
            len(self.encoding.encode(str(content)[: content.run_end], disallowed_special=()))
            for message in kwargs.get("messages") or [{}]
            if isinstance(content := message.get("content"), CacheablePrompt)
        )
        if schema := (kwargs.get("response_format") or {}).get("json_schema"):
            # Free routes may put the same required schema into message text.
            tokens += len(
                self.encoding.encode(_inject_schema_into_prompt("", schema), disallowed_special=())
            )
        for field in ("tools", "functions"):
            if definitions := kwargs.get(field):
                tokens += len(self.encoding.encode(json.dumps(definitions), disallowed_special=()))
        started = time.perf_counter()
        try:
            return await self.inner.complete(**kwargs)
        finally:
            self.attempts[attempt] = (tokens, prefix_tokens, (time.perf_counter() - started) / 60)

    def supports_json_schema(self, model_name: str) -> bool:
        return bool(self.inner.supports_json_schema(model_name))


def _prompt_usage(
    exporter: InMemorySpanExporter, recorder: _PromptRecorder
) -> dict[str, _PromptUsage]:
    spans = {span.context.span_id: span for span in exporter.get_finished_spans()}
    usage: dict[str, _PromptUsage] = collections.defaultdict(_PromptUsage)
    for span in spans.values():
        if not span.name.startswith("chat "):
            continue
        task = meter._ancestor(span, "task.execute", spans)
        call = meter._ancestor(span, "llm.call", spans)
        node = str(task.attributes.get("co_scientist.task.node", "")) if task else ""
        prompt = str(call.attributes.get("co_scientist.llm.prompt_name", "")) if call else ""
        prompt = meter._INDEX.sub("", meter._UUID.sub("", prompt)) or "unnamed"
        phase = meter._phase(node, prompt)
        row = usage[f"{phase}/{node.removeprefix('engine.')}/{prompt}"]
        attempt = span.parent.span_id if span.parent else 0
        assert attempt in recorder.attempts, "a call escaped the measured offline backend"
        tokens, prefix_tokens, minutes = recorder.attempts[attempt]
        row.calls += 1
        row.estimated_tokens += tokens
        row.max_tokens = max(row.max_tokens, tokens)
        row.cacheable_prefix_tokens += prefix_tokens
        row.max_cacheable_prefix_tokens = max(row.max_cacheable_prefix_tokens, prefix_tokens)
        row.completion_minutes += minutes
    return dict(usage)


@pytest.fixture(autouse=True)
def _offline_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "_installed", backend._installed)
    monkeypatch.setattr(offline_llm, "_installed", False)


@pytest.mark.parametrize("tier", list(meter.CEILINGS))
def test_every_call_fits_its_prompt_envelope(
    tier: str,
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_process_mode: FakeProcessMode,
) -> None:
    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("prompt envelopes must not use the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(files("litellm.litellm_core_utils.tokenizers")))
    monkeypatch.setattr(settings, "claim_verifier_model", offline_llm.DEFAULT_OFFLINE_MODEL)
    exporter, original = meter._install_production_shape(monkeypatch, fake_process_mode)
    monkeypatch.setattr(mcp_client, "MultiServerMCPClient", _PromptMCPClient)
    recorder = _PromptRecorder(original)
    backend.install_backend(recorder)
    run = seed_run(
        "Explain how molecular crowding changes the folding pathway of protein X.",
        profile=tier,
        provider="mock",
        config={"tier": tier, "llm_backend": "offline"},
        client_id=f"prompt-envelope-{tier}",
        llm_backend="offline",
        db_path=isolated_db,
    )
    started = time.perf_counter()
    drive_offline_run(run, db_path=isolated_db, worker=f"prompt-envelope-{tier}")
    final = runs.get_run(run.id, db_path=isolated_db)
    assert final is not None and final.status == RunStatus.COMPLETED.value
    measured = meter._measure(exporter, original)
    prompt_usage = _prompt_usage(exporter, recorder)
    assert prompt_usage, "an empty meter cannot establish a prompt ceiling"
    assert any(row.cacheable_prefix_tokens for row in prompt_usage.values()), (
        "trusted cache boundaries were lost before dispatch"
    )
    assert measured["phases"].get("claims", {}).get("calls", 0), "claim checks were skipped"
    assert any("literature_review" in node for _, node, *_ in measured["calls_by_prompt"]), (
        "literature analysis was skipped"
    )
    if out := os.environ.get("METER_OUT"):
        with open(os.path.join(out, f"{tier}-prompts.json"), "w") as handle:
            json.dump(
                {
                    "tier": tier,
                    **measured,
                    "elapsed_minutes": (time.perf_counter() - started) / 60,
                    "estimator": (
                        "cl100k_base message content, schemas at inline-fallback size and tool "
                        "definitions; excludes provider framing"
                    ),
                    "prompt_usage": {
                        key: dataclasses.asdict(row) for key, row in prompt_usage.items()
                    },
                },
                handle,
                indent=1,
            )
    oversized = {
        key: row.max_tokens
        for key, row in prompt_usage.items()
        if row.max_tokens > PROMPT_TOKEN_CEILING
    }
    assert not oversized, f"{tier} prompts exceed {PROMPT_TOKEN_CEILING}: {oversized}"
