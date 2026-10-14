import json
import re
from collections import Counter
from typing import Any

_ELISION = "\n[Context excerpt shortened; omitted text is not evidence of absence.]\n"
_TURN = re.compile(r"(?=\n*Turn \d+:\n)")
_WORDS = re.compile(r"[a-zA-Z]{3,}")
_RECORD_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def bounded_excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_ELISION):
        return ""
    available = limit - len(_ELISION)
    head = available * 2 // 3
    tail = available - head
    return text[:head].rsplit(" ", 1)[0] + _ELISION + text[-tail:].split(" ", 1)[-1]


def summarize_transcript(transcript: str, limit: int = 8_000) -> str:
    if len(transcript) <= limit:
        return transcript
    turns = [turn.strip() for turn in _TURN.split(transcript) if turn.strip()]
    if not turns:
        return ""
    per_turn = max(0, (limit - 2 * len(turns)) // len(turns))
    return "\n\n".join(bounded_excerpt(turn, per_turn) for turn in turns)


def select_evidence_excerpt(evidence: str, query: str, limit: int = 10_000) -> str:
    if len(evidence) <= limit:
        return evidence
    # Source handles travel with each selected passage; cutting the final
    # assembled corpus could leave a dangling title or citation identifier.
    records = re.split(r"\n(?=- evidence-\d+:)", evidence)
    if len(records) == 1:
        records = re.split(r"\n\s*\n", evidence)
    terms = set(_WORDS.findall(query.casefold()))
    indexed = list(enumerate(records))
    indexed.sort(key=lambda item: (-len(terms & set(_WORDS.findall(item[1].casefold()))), item[0]))
    selected: list[tuple[int, str]] = []
    remaining = limit - len(_ELISION)
    for index, record in indexed:
        excerpt = bounded_excerpt(record.strip(), min(700, remaining - 1))
        if not excerpt:
            break
        selected.append((index, excerpt))
        remaining -= len(excerpt) + 1
    selected.sort()
    return "\n".join(excerpt for _, excerpt in selected) + _ELISION


def summarize_hypotheses(summary: str, limit: int = 6_000) -> str:
    records = re.split(r"\n(?=(?:\d+\. \(Elo |Idea \d+:))", summary)
    if len(summary) <= limit:
        return summary
    per_record = max(0, (limit - len(records)) // len(records))
    return "\n".join(bounded_excerpt(record, per_record) for record in records)


def summarize_references(reference_list: str, limit: int = 4_000) -> str:
    if len(reference_list) <= limit:
        return reference_list
    references = re.findall(r"^(\[C\d+\])\s*(.*)$", reference_list, re.M)
    if not references:
        return bounded_excerpt(reference_list, limit)
    available = max(0, limit - sum(len(key) + 2 for key, _ in references))
    per_label = available // len(references)
    lines = []
    for key, label in references:
        if len(label) > per_label and " — " in label:
            label = label.split(" — ", 1)[1]
        if len(label) > per_label:
            marker = " [excerpt] "
            prose = max(0, per_label - len(marker))
            head = prose * 2 // 3
            label = label[:head] + marker + label[-(prose - head) :] if prose else ""
        lines.append(f"{key} {label}")
    return "\n".join(lines)


def compact_json_context(serialized: str, limit: int = 6_000) -> str:
    try:
        data = json.loads(serialized)
    except (ValueError, TypeError):
        return bounded_excerpt(serialized, limit)

    def shorten(value: object, allowance: int, field: str = "") -> object:
        if field in {"id", "verdict", "classification", "columns", "score_axes"} or field.endswith(
            ("_id", "_verdict", "_classification", "_disposition")
        ):
            return value
        if isinstance(value, str):
            if len(value) <= allowance:
                return value
            marker = " [excerpt] "
            available = max(0, allowance - len(marker))
            head = available * 2 // 3
            return value[:head] + marker + value[-(available - head) :]
        if isinstance(value, dict):
            if "columns" in value and "rows" in value:
                return {
                    **value,
                    "rows": [
                        [
                            shorten(item, allowance, str(key))
                            for key, item in zip(value["columns"], row, strict=True)
                        ]
                        for row in value["rows"]
                    ],
                }
            return {key: shorten(item, allowance, str(key)) for key, item in value.items()}
        if isinstance(value, list):
            return [shorten(item, allowance, field) for item in value]
        return value

    # Keep keys, numeric assessments and record positions even when prose must
    # shrink; arbitrary slicing would produce invalid JSON and lose verdicts.
    allowance = 700
    result = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    while len(result) > limit:
        result = json.dumps(shorten(data, allowance), ensure_ascii=False, separators=(",", ":"))
        if allowance == 16:
            break
        allowance = max(16, allowance // 2)
    return result


def summarize_feedback(serialized: str, limit: int = 10_000) -> str:
    try:
        records = json.loads(serialized)
    except (ValueError, TypeError):
        return bounded_excerpt(serialized, limit)
    if not isinstance(records, list):
        return compact_json_context(serialized, limit)
    summaries = []
    for record in records:
        if not isinstance(record, dict):
            summaries.append(record)
            continue
        summary = dict(record)
        reviews = summary.get("reviews")
        if isinstance(reviews, list) and reviews:
            summary["review_count"] = len(reviews)
            latest = reviews[-1]
            summary["reviews"] = (
                [
                    {
                        key: latest[key]
                        for key in (
                            "review_summary",
                            "scores",
                            "safety_ethical_concerns",
                            "constructive_feedback",
                            "overall_score",
                            "reviewer",
                            "score",
                            "reasoning",
                        )
                        if key in latest
                    }
                ]
                if isinstance(latest, dict)
                else reviews[-1:]
            )
        turns = summary.get("debate_transcript")
        if isinstance(turns, list) and turns:
            summary["debate_transcript"] = {
                key: [turn.get(key) for turn in turns if isinstance(turn, dict)]
                for key in ("turn", "presentation_order", "valid_output")
            }
            summary["debate_transcript"]["winner_side"] = [
                "a"
                if turn.get("winner_id") == summary.get("hypothesis_a_id")
                else "b"
                if turn.get("winner_id") == summary.get("hypothesis_b_id")
                else turn.get("winner_id")
                for turn in turns
                if isinstance(turn, dict)
            ]
        # Match IDs identify the same ideas already included in review_history.
        if summary.get("record_type") == "ranking_debate":
            summary.pop("hypothesis_a", None)
            summary.pop("hypothesis_b", None)
        summaries.append(summary)
    # Share field names once rather than repeating the same review schema for
    # every idea and the same match schema for every pair.
    grouped: dict[str, list[dict[str, object]]] = {}
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        kind = str(summary.pop("record_type", "feedback"))
        if kind == "review_history":
            latest_reviews = summary.pop("reviews", [])
            if latest_reviews and isinstance(latest_reviews[0], dict):
                summary.update({f"latest_{key}": value for key, value in latest_reviews[0].items()})
        grouped.setdefault(kind, []).append(summary)
    tables = _feedback_tables(grouped)
    return compact_json_context(json.dumps(_compact_feedback_identifiers(tables)), limit)


def _compact_feedback_identifiers(tables: dict[str, Any]) -> dict[str, Any]:
    # Repeated UUIDs dominate the essential skeleton after prose reaches its
    # floor; references preserve full identifiers and winner-count keys.
    strings: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(tables)
    counts = Counter(value for value in strings if _RECORD_UUID.fullmatch(value))
    identifiers = sorted(value for value, count in counts.items() if count > 1)
    if not identifiers:
        return tables
    reserved = set(strings)
    prefix = "record"
    while any(f"{prefix}_{index}_id" in reserved for index in range(1, len(identifiers) + 1)):
        prefix = "_" + prefix
    aliases = {value: f"{prefix}_{index}_id" for index, value in enumerate(identifiers, start=1)}

    def replace(value: Any) -> Any:
        if isinstance(value, str):
            return aliases.get(value, value)
        if isinstance(value, dict):
            return {aliases.get(key, key): replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value

    result: dict[str, Any] = replace(tables)
    field = "record_ids"
    while field in result:
        field = "_" + field
    result[field] = {alias: value for value, alias in aliases.items()}
    return result


def _feedback_tables(grouped: dict[str, list[dict[str, object]]]) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    matches = grouped.get("ranking_debate", [])
    if len(matches) > 8:
        tables["ranking_history_summary"] = {
            "match_count": len(matches),
            "winner_counts": dict(Counter(str(match.get("winner_id")) for match in matches)),
            "detail": "Latest eight matches follow; idea win/loss records cover the full history.",
        }
        grouped["ranking_debate"] = matches[-8:]
    for kind, entries in grouped.items():
        columns = list(dict.fromkeys(key for entry in entries for key in entry))
        score_sets = [
            scores for entry in entries if isinstance(scores := entry.get("latest_scores"), dict)
        ]
        score_axes = list(dict.fromkeys(axis for scores in score_sets for axis in scores))
        for entry in entries:
            scores = entry.get("latest_scores")
            if isinstance(scores, dict):
                entry["latest_scores"] = [scores.get(axis) for axis in score_axes]
        tables[kind] = {
            "columns": columns,
            "score_axes": score_axes,
            "rows": [[entry.get(key) for key in columns] for entry in entries],
        }
    return tables
