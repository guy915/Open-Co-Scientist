"""Does a retrieved span answer the question it was fetched to answer?

Distinct from `citation_eval.py`, and the distinction is the whole point.
That one asks whether a span *entails a claim* -- the question a citation
answers once it is in a report. This one asks whether a span answers the
*question the search was serving*, which is a fact nothing in this repo
could express until deep research made the question a first-class
artifact and Stage B persisted it beside the query
(``retrieval_calls.question``).

The two come apart in the case that matters most for a research loop: a
span on exactly the right subject that answers a different question. A
paper about embryonic expression is squarely on-topic for a question
about adult expression and answers none of it. That is the shape of
retrieval failure a follow-up round exists to catch, and it is invisible
to any measure of topical overlap -- which is also why the deterministic
baseline here is expected to score badly on precisely those items and is
kept as a floor rather than as the answer.

Labels: ``useful`` (answers the question, in either direction -- a span
saying the trial failed is as useful as one saying it succeeded),
``partial`` (bears on the question without settling it), ``useless``
(same topic, different question, or no bearing).

**Offline by default**: the deterministic baseline is lexical coverage of
the question's content words, needs nothing, and is a floor. ``--llm``
scores a real model against the same panel and is the mode worth
believing; it needs a provider key. Unlike the entailment panel there is
no production assessor to score here -- the system does not yet judge its
own retrievals this way, so this eval's judge is the eval's own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT / "app") not in sys.path:
    sys.path.insert(0, str(_ROOT / "app"))

from evaluations._artifacts import write_dated_artifact  # noqa: E402

_DATASET = pathlib.Path(__file__).parent / "datasets" / "citation_usefulness_v1.json"

_LABELS = ("useful", "partial", "useless")

# Words carrying no topic, so their presence in a span says nothing about
# whether it answers anything.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "reported",
        "that",
        "the",
        "there",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "which",
        "who",
        "why",
        "with",
    ]
)

_JUDGE_SCHEMA: dict[str, Any] = {
    "name": "citation_usefulness",
    "schema": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": list(_LABELS)},
            "reason": {"type": "string"},
        },
        "required": ["label", "reason"],
        "additionalProperties": False,
    },
}

_JUDGE_PROMPT = """You are scoring one retrieved passage against the question \
a literature search was trying to answer.

Question the search was serving:
{question}

Passage retrieved:
{span}

Label the passage:
- "useful": it answers the question, in either direction. A passage \
reporting that something failed answers the question as well as one \
reporting that it worked.
- "partial": it bears on the question without settling it (adjacent \
evidence, a different species, a related measurement).
- "useless": it is about the same topic but answers a different question, \
or has no bearing at all.

Judge only whether it answers THIS question. A passage can be interesting, \
correct and on-topic and still be useless here."""


def load_dataset(path: pathlib.Path = _DATASET) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def deterministic_label(question: str, span: str) -> str:
    """Vocabulary overlap cannot establish that a passage answers the
    question.
    """
    from co_scientist.core.text_matching import coverage

    wanted = _content_words(question)
    if not wanted:
        return "useless"
    overlap = coverage(wanted, _content_words(span))
    if overlap >= 0.5:
        return "useful"
    return "partial" if overlap >= 0.25 else "useless"


def _content_words(text: str) -> set[str]:
    from co_scientist.core.text_matching import tokenize

    return set(tokenize(text, min_len=3, stopwords=_STOPWORDS))


def score(labelled: list[tuple[str, str, str]]) -> dict[str, Any]:
    total = len(labelled)
    correct = sum(1 for _, want, got in labelled if want == got)
    per_label = {
        label: _recall(labelled, label)
        for label in _LABELS
        if any(want == label for _, want, _ in labelled)
    }
    useless = [t for t in labelled if t[1] == "useless"]
    return {
        "n": total,
        "accuracy": round(correct / total, 4) if total else None,
        "recall_by_label": per_label,
        "false_useful_rate": (
            round(sum(1 for t in useless if t[2] == "useful") / len(useless), 4)
            if useless
            else None
        ),
    }


def _recall(labelled: list[tuple[str, str, str]], label: str) -> float:
    items = [t for t in labelled if t[1] == label]
    return round(sum(1 for t in items if t[2] == label) / len(items), 4)


def run_deterministic(dataset: dict[str, Any]) -> dict[str, Any]:
    return _run(dataset, "deterministic_coverage", live=False)


def run_llm(dataset: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    from evaluations._live_config import configure_live_environment

    return _run(dataset, configure_live_environment(model), live=True)


def _run(dataset: dict[str, Any], model: str, *, live: bool) -> dict[str, Any]:
    from evaluations._identity import capture_panel

    with capture_panel("citation_usefulness", dataset, model, live=live) as evidence:
        labelled = (
            asyncio.run(_judge_all(dataset, model))
            if live
            else [
                (
                    str(item["id"]),
                    str(item["label"]),
                    deterministic_label(str(item["question"]), str(item["span"])),
                )
                for item in dataset["items"]
            ]
        )
    return {
        **evidence,
        "judge": model,
        "panel": dataset["name"],
        "metrics": score(labelled),
        "items": [{"id": i, "expected": want, "predicted": got} for i, want, got in labelled],
    }


async def _judge_all(dataset: dict[str, Any], model: str) -> list[tuple[str, str, str]]:
    from co_scientist.platform.llm import CompletionSpec, call_llm_json

    spec = CompletionSpec(model_name=model, temperature=0.0, json_schema=_JUDGE_SCHEMA)
    out: list[tuple[str, str, str]] = []
    for item in dataset["items"]:
        answer = await call_llm_json(
            _JUDGE_PROMPT.format(question=item["question"], span=item["span"]),
            spec,
        )
        predicted = str(answer.get("label") or "")
        out.append(
            (
                str(item["id"]),
                str(item["label"]),
                predicted if predicted in _LABELS else "useless",
            )
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Score a real model instead of the lexical floor.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Explicit OpenRouter model; otherwise use MODEL_NAME.",
    )
    args = parser.parse_args()

    dataset = load_dataset()
    report = run_llm(dataset, args.model) if args.llm else run_deterministic(dataset)
    tag = report["judge"].replace("/", "_").replace(":", "_")
    out = write_dated_artifact(report, f"citation-usefulness-{tag}")

    m = report["metrics"]
    print(
        f"citation usefulness [{report['judge']}]: n={m['n']} "
        f"accuracy={m['accuracy']} "
        f"false_useful_rate={m['false_useful_rate']}"
    )
    print(f"recall by label: {m['recall_by_label']}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
