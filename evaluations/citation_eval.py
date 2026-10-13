"""Synthetic labels are not representative calibration; human-audited data
remains an external gap.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from typing import Any

from evaluations._artifacts import write_dated_artifact

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DATASET = _ROOT / "evaluations" / "datasets" / "citation_entailment_v1.json"
_CHALLENGE_DATASET = _ROOT / "evaluations" / "datasets" / "citation_entailment_challenge_v1.json"

# Contradiction misses threaten publication; conservative abstention costs
# recall. These are reconstructed gates, not Google's undisclosed thresholds.
_PRODUCTION_GATES = {
    "contradiction_recall": 0.80,
    "accuracy": 0.75,
}

sys.path.insert(0, str(_ROOT / "app"))


def _load_dataset(path: pathlib.Path) -> dict[str, Any]:
    dataset: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return dataset


def _predict(
    items: list[dict[str, Any]],
    assessor: Any,
    assessor_id: str,
) -> list[tuple[str, str, str]]:
    """Assess once per item: semantic judgments incur a provider call."""
    from co_scientist.domains.research_state.claims import as_passages, assess_claim

    rows: list[tuple[str, str, str]] = []
    for item in items:
        passages = as_passages(item["passages"])
        predicted = assess_claim(
            item["claim"],
            passages,
            assessor=assessor,
            assessor_id=assessor_id,
        ).label.value
        rows.append((item["label"], predicted, item.get("kind", "obvious")))
    return rows


def _confusion(rows: list[tuple[str, str, str]]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for expected, predicted, _kind in rows:
        matrix[expected][predicted] += 1
    return {k: dict(v) for k, v in matrix.items()}


def _accuracy_by_kind(
    rows: list[tuple[str, str, str]],
) -> dict[str, dict[str, Any]]:
    """Lexical/hard-paraphrase splits expose the deterministic baseline's
    semantic gap.
    """
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for expected, predicted, kind in rows:
        counts[kind][1] += 1
        if predicted == expected:
            counts[kind][0] += 1
    return {
        kind: {
            "accuracy": round(correct / total, 3) if total else 0.0,
            "n": total,
        }
        for kind, (correct, total) in sorted(counts.items())
    }


def _metrics(matrix: dict[str, dict[str, int]]) -> dict[str, Any]:
    labels = {"supports", "contradicts", "insufficient"}
    total = sum(sum(row.values()) for row in matrix.values())
    correct = sum(matrix.get(lbl, {}).get(lbl, 0) for lbl in labels)

    per_label: dict[str, dict[str, float]] = {}
    for lbl in labels:
        tp = matrix.get(lbl, {}).get(lbl, 0)
        predicted_lbl = sum(matrix.get(exp, {}).get(lbl, 0) for exp in matrix)
        actual_lbl = sum(matrix.get(lbl, {}).values())
        precision = tp / predicted_lbl if predicted_lbl else 0.0
        recall = tp / actual_lbl if actual_lbl else 0.0
        per_label[lbl] = {
            "precision": round(precision, 3),
            "recall": round(recall, 3),
        }

    return {
        "accuracy": round(correct / total, 3) if total else 0.0,
        "contradiction_recall": per_label["contradicts"]["recall"],
        "abstention_rate": round(
            sum(matrix.get(exp, {}).get("insufficient", 0) for exp in matrix) / total,
            3,
        )
        if total
        else 0.0,
        "per_label": per_label,
        "n": total,
    }


def _gate_report(metrics: dict[str, Any]) -> dict[str, Any]:
    checks = {
        name: {
            "value": metrics[name],
            "threshold": threshold,
            "passed": metrics[name] >= threshold,
        }
        for name, threshold in _PRODUCTION_GATES.items()
    }
    return {
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
    }


def run(
    *,
    use_llm: bool = False,
    dataset_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    assessor, assessor_id = _selected_assessor(use_llm)
    dataset = _load_dataset(dataset_path or _DATASET)
    from evaluations._identity import capture_panel

    with capture_panel(
        "citation_entailment",
        dataset,
        assessor_id.removeprefix("llm:"),
        live=use_llm,
    ) as evidence:
        rows = _predict(dataset["items"], assessor, assessor_id)
    matrix = _confusion(rows)
    metrics = _metrics(matrix)
    metrics["by_kind"] = _accuracy_by_kind(rows)
    return {
        **evidence,
        "dataset": dataset["name"],
        "dataset_version": dataset["version"],
        "assessor": assessor_id,
        "metrics": metrics,
        "confusion": matrix,
        "production_gates": _gate_report(metrics),
        "external_gap": (
            "Human-audited representative sample and threshold calibration not "
            "performed (no annotator panel); synthetic dataset only. "
            "Retraction handling is upstream of this assessor (retracted "
            "sources are "
            "quarantined before grounding), so a retracted-passage item scored "
            "here isolates only the assessor, not the full pipeline."
        ),
    }


def _selected_assessor(use_llm: bool) -> tuple[Any, str]:
    if use_llm:
        return _build_llm_assessor()
    from co_scientist.domains.research_state.claims import deterministic_assessor

    return deterministic_assessor, "deterministic-v1"


def _build_llm_assessor() -> tuple[Any, str]:
    from evaluations._live_config import configure_live_environment

    model = configure_live_environment()
    from co_scientist.domains.research_state.claims.verifier import make_llm_assessor

    assessor, assessor_id = make_llm_assessor(model)
    return assessor, str(assessor_id)


def _run_selected_assessor(
    dataset_path: pathlib.Path,
) -> tuple[dict[str, Any], str]:
    if "--llm" in sys.argv[1:]:
        report = run(use_llm=True, dataset_path=dataset_path)
        assessor_id = str(report["assessor"])
        return report, assessor_id.replace("/", "_").replace(":", "_")
    return run(dataset_path=dataset_path), "deterministic"


def main() -> int:
    challenge = "--challenge" in sys.argv[1:]
    dataset_path = _CHALLENGE_DATASET if challenge else _DATASET
    panel = "challenge" if challenge else "v1"

    report, tag = _run_selected_assessor(dataset_path)
    suffix = f"-{panel}" if challenge else ""
    out = write_dated_artifact(report, f"citation-entailment{suffix}-{tag}")

    m = report["metrics"]
    print(
        f"citation-entailment eval [{report['assessor']}] panel={panel}: "
        f"n={m['n']} accuracy={m['accuracy']} "
        f"contradiction_recall={m['contradiction_recall']}"
    )
    print(f"by kind: {m['by_kind']}")
    gates = report["production_gates"]
    print(f"production gates passed: {gates['passed']} ({gates['checks']})")
    print(f"wrote {out}")
    # The lexical baseline is expected to fail semantic gates; enforce them only
    # for the real assessor.
    if challenge and "--llm" in sys.argv[1:] and not gates["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
