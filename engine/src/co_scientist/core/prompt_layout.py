from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from co_scientist.core.prompt_cache import CacheablePrompt

_snapshots: OrderedDict[tuple[str, str], str] = OrderedDict()
_snapshot_lock = threading.Lock()
_erased_runs: OrderedDict[bytes, None] = OrderedDict()
_memoization_disabled = False
_MAX_SNAPSHOTS = 512
_MAX_SNAPSHOT_CHARS = 16_000


def stable_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (set, frozenset)):
        return json.dumps(
            sorted(value, key=_json_sort_key), ensure_ascii=False, default=_json_default
        )
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=_json_default)


def _json_sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> Any:
    return sorted(value, key=_json_sort_key) if isinstance(value, (set, frozenset)) else str(value)


def shared_evidence(run_id: str | None, field: str, text: str) -> tuple[str, str]:
    # A changed corpus stays visible after the prefix; never discard new
    # findings merely to reuse a cached baseline.
    if not run_id or not text or len(text) > _MAX_SNAPSHOT_CHARS:
        return text, ""
    key = (run_id, field)
    with _snapshot_lock:
        if _memoization_disabled or hashlib.sha256(run_id.encode()).digest() in _erased_runs:
            return text, ""
        baseline = _snapshots.setdefault(key, text)
        _snapshots.move_to_end(key)
        while len(_snapshots) > _MAX_SNAPSHOTS:
            _snapshots.popitem(last=False)
    return baseline, text if text != baseline else ""


def retire_run_prompt_context(run_id: str) -> None:
    global _memoization_disabled
    marker = hashlib.sha256(run_id.encode()).digest()
    with _snapshot_lock:
        for key in [key for key in _snapshots if key[0] == run_id]:
            del _snapshots[key]
        if _memoization_disabled:
            return
        # Never evict an erasure marker and reopen late private memoization.
        if marker not in _erased_runs and len(_erased_runs) >= _MAX_SNAPSHOTS:
            _snapshots.clear()
            _erased_runs.clear()
            _memoization_disabled = True
            return
        _erased_runs[marker] = None
        _erased_runs.move_to_end(marker)


def render_cacheable_prompt(
    instructions: str,
    run: str,
    item: str,
    question: str = "Apply the instructions above to the supplied inputs.",
) -> CacheablePrompt:
    prefix = instructions.rstrip() + "\n\n## Shared\n" + run.rstrip() + "\n\n"
    body = prefix + "## Current\n" + item.rstrip() + "\n\n"
    return CacheablePrompt(
        body + "## Question\n" + question,
        run_end=len(prefix),
        item_end=len(body),
    )


def render_fields(fields: Mapping[str, Any]) -> str:
    labels = {
        "hypothesis_a": "Hypothesis 1:\n",
        "hypothesis_b": "Hypothesis 2:\n",
        "original_hypothesis": "**Original Hypothesis:**\n",
        "theme_title": "Theme to write: ",
    }
    return "\n\n".join(
        labels.get(key, key + ":\n") + stable_value(fields[key])
        for key in sorted(fields)
        if fields[key] != ""
    )


def prepend_instructions(prompt: str, instructions: str) -> str:
    if isinstance(prompt, CacheablePrompt):
        return CacheablePrompt(
            instructions + str(prompt),
            run_end=len(instructions) + prompt.run_end,
            item_end=len(instructions) + prompt.item_end,
        )
    return instructions + prompt


def append_item_context(prompt: str, context: str) -> str:
    if not context:
        return prompt
    if isinstance(prompt, CacheablePrompt):
        addition = context.strip("\n") + "\n\n"
        return CacheablePrompt(
            str(prompt)[: prompt.item_end] + addition + str(prompt)[prompt.item_end :],
            run_end=prompt.run_end,
            item_end=prompt.item_end + len(addition),
        )
    return prompt + context
