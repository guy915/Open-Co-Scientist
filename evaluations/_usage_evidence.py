import copy
import math
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def summarize_usage(usage: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Complete cost estimates need observed model, tokens and price for
    every physical call. Empty telemetry proves neither free execution nor
    absent inference.
    """
    calls = sum(int(row.get("calls", 0)) for row in usage.values())
    missing = _missing_evidence_counts(usage)
    fallbacks: Counter[str] = Counter()
    requested: Counter[str] = Counter()
    for row in usage.values():
        requested.update(row.get("requested_models", {}))
        fallbacks.update(row.get("deterministic_fallbacks", {}))
    partial_estimate = sum(float(row["cost_usd"]) for row in usage.values() if _valid_cost(row))
    complete = calls > 0 and not any(missing.values())
    return {
        "model_usage": copy.deepcopy(usage),
        "physical_calls": calls,
        "recorded_deterministic_fallbacks": dict(sorted(fallbacks.items())),
        "fallback_evidence": "recorded_events_only",
        "requested_models": dict(sorted(requested.items())),
        "observed_models": sorted(
            {
                key.split("::", 1)[-1]
                for key, row in usage.items()
                if row.get("observed_model_calls", 0) > 0
            }
        ),
        **missing,
        "has_usage_records": bool(usage),
        "estimate_complete": complete,
        "estimated_total_usd": round(partial_estimate, 6) if complete else None,
        "partial_estimated_total_usd": round(partial_estimate, 6),
        "billed_total_usd": None,
        "cost_basis": "static_estimate_not_billing_receipt",
    }


def _missing_evidence_counts(
    usage: dict[str, dict[str, Any]],
) -> dict[str, int]:
    return {
        label: sum(
            max(0, int(row.get("calls", 0)) - _evidenced_calls(row, field))
            for row in usage.values()
        )
        for label, field in (
            ("unobserved_model_calls", "observed_model_calls"),
            ("unreported_usage_calls", "reported_usage_calls"),
            ("unpriced_calls", "priced_usage_calls"),
        )
    }


def _valid_cost(row: dict[str, Any]) -> bool:
    value = row.get("cost_usd")
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _evidenced_calls(row: dict[str, Any], field: str) -> int:
    if field == "priced_usage_calls" and not _valid_cost(row):
        return 0
    value = int(row.get(field, 0))
    return value if 0 <= value <= int(row.get("calls", 0)) else 0


@contextmanager
def capture_usage(phase: str, *, live: bool) -> Iterator[dict[str, Any]]:
    evidence: dict[str, Any] = {
        "execution_mode": "live_requested" if live else "offline",
        "usage_evidence": None,
    }
    if not live:
        yield evidence
        return
    from co_scientist.platform.llm import scoped_telemetry

    with scoped_telemetry(phase) as telemetry:
        try:
            yield evidence
        finally:
            evidence["usage_evidence"] = summarize_usage(telemetry.snapshot())
