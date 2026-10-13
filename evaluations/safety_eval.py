"""Synthetic adversarial/control labels do not measure Google's private
benchmark.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from evaluations._artifacts import write_dated_artifact

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DATASETS_DIR = _ROOT / "evaluations" / "datasets"
_ADVERSARIAL_DATASET = _DATASETS_DIR / "hypothesis_safety_adversarial_v1.json"
_CONTROLS_DATASET = _DATASETS_DIR / "hypothesis_safety_controls_v1.json"

# Headline metrics measure the deterministic layer; the permissive arm is a
# bound, not an observed model.
_LAYER = (
    "deterministic (regex, no LLM call) -- "
    "co_scientist.safety.review_hypothesis_safety; the contextual "
    "assessor is bounded, not measured (see permissive_assessor_ceiling)"
)

sys.path.insert(0, str(_ROOT / "app"))


@dataclass(frozen=True)
class _ScoredItem:
    category: str
    difficulty: str
    should_block: bool
    blocked: bool

    @property
    def correct(self) -> bool:
        return self.blocked == self.should_block


def _blocked(text: str, *, permissive_assessor: bool) -> bool:
    """The permissive arm clears only holds the real eligibility gate admits;
    it bounds a hypothetical assessor.
    """
    from co_scientist.domains.safety.hypothesis.safety import (
        is_resolvable_hold,
        review_hypothesis_safety,
    )

    review = review_hypothesis_safety(text)
    if permissive_assessor and is_resolvable_hold(review):
        return False
    return bool(review.blocks_tournament)


def _score(items: list[dict[str, Any]], *, permissive_assessor: bool = False) -> list[_ScoredItem]:
    return [
        _ScoredItem(
            category=item["category"],
            difficulty=item.get("difficulty", "easy"),
            should_block=bool(item["should_block"]),
            blocked=_blocked(item["text"], permissive_assessor=permissive_assessor),
        )
        for item in items
    ]


def _rate(rows: list[_ScoredItem], *, should_block: bool) -> float:
    subset = [row for row in rows if row.should_block == should_block]
    if not subset:
        return 0.0
    wrong = sum(1 for row in subset if not row.correct)
    return round(wrong / len(subset), 3)


def _confusion(rows: list[_ScoredItem]) -> dict[str, Any]:
    unsafe = [row for row in rows if row.should_block]
    safe = [row for row in rows if not row.should_block]
    return {
        "n": len(rows),
        "n_adversarial": len(unsafe),
        "n_control": len(safe),
        "false_positive_rate": _rate(rows, should_block=False),
        "false_negative_rate": _rate(rows, should_block=True),
    }


def _grouped(rows: list[_ScoredItem], key: str) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
    for row in rows:
        bucket = groups[getattr(row, key)]
        bucket["total"] += 1
        bucket["correct"] += int(row.correct)
    return dict(groups)


def _by_difficulty(rows: list[_ScoredItem]) -> dict[str, Any]:
    return {
        difficulty: _confusion([r for r in rows if r.difficulty == difficulty])
        for difficulty in sorted({r.difficulty for r in rows})
    }


def _metrics(rows: list[_ScoredItem], permissive: list[_ScoredItem]) -> dict[str, Any]:
    """Offline arms bound contextual resolution; neither measures a real
    assessor's judgment.
    """
    return {
        **_confusion(rows),
        "by_difficulty": _by_difficulty(rows),
        "per_category": _grouped(rows, "category"),
        "permissive_assessor_ceiling": {
            **_confusion(permissive),
            "by_difficulty": _by_difficulty(permissive),
        },
    }


def run() -> dict[str, Any]:
    adversarial = json.loads(_ADVERSARIAL_DATASET.read_text(encoding="utf-8"))
    controls = json.loads(_CONTROLS_DATASET.read_text(encoding="utf-8"))
    items = adversarial["items"] + controls["items"]

    return {
        "layer": _LAYER,
        "adversarial_dataset": adversarial["name"],
        "adversarial_dataset_version": adversarial["version"],
        "controls_dataset": controls["name"],
        "controls_dataset_version": controls["version"],
        "policy_version": _policy_version(),
        "metrics": _metrics(_score(items), _score(items, permissive_assessor=True)),
        "external_gap": (
            "Does not reproduce Google's private 1,200-goal safety benchmark "
            "(request-only); synthetic sets for false-positive / "
            "false-negative analysis only. Does not exercise the optional "
            "semantic (LLM) safety escalation layered on top in the app."
        ),
    }


def _policy_version() -> str:
    from co_scientist.domains.safety.hypothesis.safety import POLICY_VERSION

    return str(POLICY_VERSION)


def main() -> int:
    report = run()
    out = write_dated_artifact(
        report,
        "hypothesis-safety",
        model=f"none ({report['layer']})",
    )

    m = report["metrics"]
    print(
        f"hypothesis-safety eval: n={m['n']} "
        f"FP_rate={m['false_positive_rate']} FN_rate={m['false_negative_rate']}"
    )
    for difficulty, sub in m["by_difficulty"].items():
        print(
            f"  difficulty={difficulty} n={sub['n']} "
            f"FP_rate={sub['false_positive_rate']} "
            f"FN_rate={sub['false_negative_rate']}"
        )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
