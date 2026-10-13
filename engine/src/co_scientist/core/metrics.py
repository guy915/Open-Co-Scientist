import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelCallStats:
    """Every field is an additive delta, not an independently cumulative
    snapshot.
    """

    deterministic_fallbacks: dict[str, int] = field(default_factory=dict)
    calls: int = 0
    observed_model_calls: int = 0
    reported_usage_calls: int = 0
    priced_usage_calls: int = 0
    requested_models: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_tokens: int = 0
    refusals: int = 0
    cost_usd: float = 0.0
    latency_seconds: float = 0.0
    retries: int = 0
    errors: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _known_field_kwargs(cls: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Ignore unknown serialized fields so older checkpoints remain loadable."""
    field_names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in field_names}


@dataclass
class ExecutionMetrics:
    total_time: float = 0.0
    hypothesis_count: int = 0
    reviews_count: int = 0
    tournaments_count: int = 0
    evolutions_count: int = 0
    llm_calls: int = 0
    phase_times: dict[str, float] = field(default_factory=dict)
    # Aggregate node telemetry in memory; per-call persistence starves SQLite's
    # single writer.
    model_usage: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Database terms can require attribution; temporary skill notices do not
    # reach the report reader.
    skills_used: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionMetrics":
        return cls(**_known_field_kwargs(cls, data))


def _merge_phase_times(
    existing_phase_times: dict[str, float], new_phase_times: dict[str, float]
) -> dict[str, float]:
    merged_phase_times = dict(existing_phase_times)

    for phase, time_val in new_phase_times.items():
        merged_phase_times[phase] = merged_phase_times.get(phase, 0.0) + time_val

    return merged_phase_times


def _merge_usage_entry(existing_entry: dict[str, Any], new_entry: dict[str, Any]) -> dict[str, Any]:
    """Every usage field is additive; derive fields so fan-out cannot
    silently zero new counters.
    """
    numeric_fields = tuple(
        f.name
        for f in dataclasses.fields(ModelCallStats)
        if f.name not in {"errors", "requested_models", "deterministic_fallbacks"}
    )
    merged = {
        field_name: existing_entry.get(field_name, 0) + new_entry.get(field_name, 0)
        for field_name in numeric_fields
    }
    merged["errors"] = _merge_counts(existing_entry.get("errors", {}), new_entry.get("errors", {}))
    merged["requested_models"] = _merge_counts(
        existing_entry.get("requested_models", {}),
        new_entry.get("requested_models", {}),
    )
    merged["deterministic_fallbacks"] = _merge_counts(
        existing_entry.get("deterministic_fallbacks", {}),
        new_entry.get("deterministic_fallbacks", {}),
    )
    return merged


def _merge_model_usage(
    existing_usage: dict[str, dict[str, Any]],
    new_usage: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged = {key: dict(entry) for key, entry in existing_usage.items()}
    for key, new_entry in new_usage.items():
        merged[key] = _merge_usage_entry(merged.get(key, {}), new_entry)
    return merged


def _merge_counts(existing: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    merged = dict(existing)
    for name, count in new.items():
        merged[name] = merged.get(name, 0) + count
    return merged


def merge_metrics(existing: ExecutionMetrics, new: ExecutionMetrics) -> ExecutionMetrics:
    merged_phase_times = _merge_phase_times(existing.phase_times, new.phase_times)

    # Hypothesis counts are running totals, unlike additive usage deltas.
    merged = ExecutionMetrics(
        hypothesis_count=max(existing.hypothesis_count, new.hypothesis_count),
        reviews_count=existing.reviews_count + new.reviews_count,
        tournaments_count=existing.tournaments_count + new.tournaments_count,
        evolutions_count=existing.evolutions_count + new.evolutions_count,
        llm_calls=existing.llm_calls + new.llm_calls,
        total_time=new.total_time if new.total_time > 0 else existing.total_time,
        phase_times=merged_phase_times,
        model_usage=_merge_model_usage(existing.model_usage, new.model_usage),
        skills_used=_merge_counts(existing.skills_used, new.skills_used),
    )

    return merged


@dataclass(frozen=True)
class MetricDeltas:
    """Reducers add node deltas, never cumulative run snapshots."""

    reviews: int = 0
    tournaments: int = 0
    evolutions: int = 0
    llm_calls: int = 0
    skills_used: Mapping[str, int] = field(default_factory=dict)


def create_metrics_update(
    hypothesis_count: int | None = None,
    deltas: MetricDeltas | None = None,
    total_time: float | None = None,
    phase_times: dict[str, float] | None = None,
    model_usage: dict[str, dict[str, Any]] | None = None,
) -> ExecutionMetrics:
    """Only additive increments belong here; a repeated cumulative snapshot
    double-counts usage.
    """
    d = deltas if deltas is not None else MetricDeltas()
    return ExecutionMetrics(
        hypothesis_count=hypothesis_count if hypothesis_count is not None else 0,
        reviews_count=d.reviews,
        tournaments_count=d.tournaments,
        evolutions_count=d.evolutions,
        llm_calls=d.llm_calls,
        total_time=total_time if total_time is not None else 0.0,
        phase_times=phase_times if phase_times is not None else {},
        model_usage=model_usage if model_usage is not None else {},
        skills_used=dict(d.skills_used),
    )


def phase_message(phase: str, content: str, **metadata: Any) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": content,
            "metadata": {"phase": phase, **metadata},
        }
    ]
