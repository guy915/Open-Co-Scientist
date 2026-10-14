from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Any

from co_scientist.core.exceptions import ProviderAdmissionError
from co_scientist.core.prompt_cache import CacheablePrompt
from co_scientist.platform.llm.admission.spend import current_spending_run_id
from co_scientist.platform.llm.roles import current_call_policy
from co_scientist.platform.telemetry.logging_setup import current_run_id

HAIKU = "anthropic/claude-haiku-5-5"
_KEYS_LIMIT = 4096
_key_lock = threading.Lock()
_key_calls: OrderedDict[str, dict[int, deque[float]]] = OrderedDict()


def _azure_cache_key(run_id: str, role: str) -> str:
    base = f"{run_id}:{role}"
    with _key_lock:
        now = time.monotonic()
        shards = _key_calls.setdefault(base, {})
        _key_calls.move_to_end(base)
        while len(_key_calls) > _KEYS_LIMIT:
            _key_calls.popitem(last=False)
        for calls in shards.values():
            while calls and calls[0] <= now - 60:
                calls.popleft()
        # Reuse the oldest partition when traffic drops; a rolling window
        # avoids doubling a burst at a wall-clock minute boundary.
        shard = next((key for key, calls in shards.items() if len(calls) < 15), len(shards))
        if shard == 256:
            shard = min(shards, key=lambda key: len(shards[key]))
        shards.setdefault(shard, deque(maxlen=15)).append(now)
        return base if shard == 0 else f"{base}:{shard}"


def apply_prompt_cache(request: dict[str, Any], model: str) -> None:
    if model != HAIKU:
        return
    messages = []
    for message in request.get("messages", ()):
        content = message.get("content")
        if isinstance(content, CacheablePrompt):
            ends = (content.run_end, content.item_end, len(content))
            blocks = []
            start = 0
            for index, end in enumerate(ends):
                text = str(content)[start:end]
                if text:
                    block: dict[str, Any] = {"type": "text", "text": text}
                    if index < 2:
                        block["cache_control"] = {"type": "ephemeral", "ttl": "5m"}
                    blocks.append(block)
                start = end
            message = {**message, "content": blocks}
        messages.append(message)
    count = sum(
        bool(block.get("cache_control"))
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict)
    )
    automatic = bool(request.get("tools")) or current_call_policy().role in ("chat", "interview")
    if count + int(automatic) > 4:
        raise ProviderAdmissionError("Too many prompt cache boundaries")
    request["messages"] = messages
    if automatic:
        request["cache_control"] = {"type": "ephemeral", "ttl": "5m"}


def apply_dispatch_cache_key(request: dict[str, Any], model: str) -> None:
    if not model.startswith("azure/"):
        return
    run_id = current_spending_run_id() or current_run_id()
    if run_id is not None:
        request["prompt_cache_key"] = _azure_cache_key(run_id, current_call_policy().role)
