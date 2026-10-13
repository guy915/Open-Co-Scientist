from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from co_scientist.core.citations import citation_keys_in
from co_scientist.domains.research_state.claims.assessor import SENTENCE_SPLIT
from co_scientist.domains.research_state.repository import records as store
from co_scientist.domains.research_state.repository.records import (
    NewCitation,
    NewEvidence,
    NewReview,
)
from co_scientist.platform.retrieval.citations import CitationRecord, classify_citation


def format_deep_verification_critique(
    probes: list[dict[str, Any]], verdict: str | None
) -> tuple[str, str]:
    summary = f"Deep verification verdict: {verdict or 'unspecified'}"
    lines = [summary, ""]
    for idx, probe in enumerate(probes, start=1):
        flag = "fundamental" if probe.get("assumption_is_fundamental") else "non-fundamental"
        lines.append(f"Probe {idx} ({flag} assumption):")
        for label, key in (
            ("Question", "question"),
            ("Answer", "answer"),
            ("Reasoning", "reasoning"),
        ):
            value = str(probe.get(key, "")).strip()
            if value:
                lines.append(f"  {label}: {value}")
        lines.append("")
    return summary, "\n".join(lines).strip()


@dataclass(frozen=True)
class _CitationSink:
    ev_id_by_title: dict[str, str]
    abstract_by_title: dict[str, str]
    citation_summary: dict[str, int]


def _ensure_citation_evidence_id(
    run_id: str,
    cite_title: str,
    cite_info: dict[str, Any],
    ev_id_by_title: dict[str, str],
    conn: sqlite3.Connection,
) -> str:
    cite_ev_id = ev_id_by_title.get(cite_title)
    if cite_ev_id is None:
        cite_ev_id = store.add_evidence(
            NewEvidence(
                run_id=run_id,
                title=cite_title,
                source=cite_info.get("type", "engine"),
                url=_citation_url(cite_info),
                authors=cite_info.get("authors") or [],
                year=cite_info.get("year"),
                abstract="",
                available=True,
            ),
            conn=conn,
        )
        ev_id_by_title[cite_title] = cite_ev_id
    return cite_ev_id


def _claim_cited_by(grounding: str, cite_key: str) -> str:
    """A source supports its cited sentences, not every claim in a multi-
    source paragraph; whole-paragraph overlap dilutes genuine support.
    """
    cited = [
        sentence
        for sentence in SENTENCE_SPLIT.split(grounding)
        if cite_key in citation_keys_in(sentence, allowed_keys={cite_key})
    ]
    return " ".join(cited).strip() or grounding


def _citation_map(h: dict[str, Any]) -> dict[str, Any]:
    return h.get("citation_map") or {}


def _citation_url(cite_info: dict[str, Any]) -> str:
    return cite_info.get("url") or ""


def _citation_available(cite_info: dict[str, Any], cite_url: str) -> bool:
    return (
        bool(cite_url)
        and not bool(cite_info.get("is_retracted"))
        and str(cite_info.get("correction_status") or "").lower() != "retracted"
    )


def _persist_one_citation(
    run_id: str,
    hyp_id: str,
    grounding: str,
    conn: sqlite3.Connection,
    cite_key: str,
    cite_info: dict[str, Any],
    sink: _CitationSink,
) -> None:
    # Non-paper sources carry display labels rather than titles; citation keys
    # are not source names.
    cite_title = cite_info.get("title") or cite_info.get("display") or cite_key
    cite_url = _citation_url(cite_info)
    cite_ev_id = _ensure_citation_evidence_id(
        run_id, cite_title, cite_info, sink.ev_id_by_title, conn
    )
    claim = f"[{cite_key}] cited in hypothesis"
    state = classify_citation(
        CitationRecord(
            url=cite_url,
            abstract=sink.abstract_by_title.get(cite_title, ""),
            claim=_claim_cited_by(grounding, cite_key),
            available=_citation_available(cite_info, cite_url),
        )
    )
    sink.citation_summary[state] += 1
    store.add_citation(
        NewCitation(
            run_id=run_id,
            hypothesis_id=hyp_id,
            evidence_id=cite_ev_id,
            claim=claim,
            state=state,
        ),
        conn=conn,
    )


def _persist_engine_citations(
    run_id: str,
    hyp_id: str,
    h: dict[str, Any],
    sink: _CitationSink,
    conn: sqlite3.Connection,
) -> None:
    grounding = str(h.get("literature_grounding") or h.get("text") or "")
    for cite_key, cite_info in _citation_map(h).items():
        _persist_one_citation(run_id, hyp_id, grounding, conn, cite_key, cite_info, sink)


# Bound malformed schema-less responses without clipping genuine structured
# review content excessively.
_MAX_DETAIL_ITEMS = 10
_MAX_DETAIL_CHARS = 500
_MAX_SHORT_CHARS = 200
# Reader-facing reviewer prose needs more room than a bulleted finding.
_MAX_PROSE_CHARS = 4000
# Scores outside the rubric's 1-10 band cannot be presented as genuine
# judgments.
_MIN_AXIS_SCORE = 1
_MAX_AXIS_SCORE = 10


def _clip_detail(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


# The app's axis copy avoids engine imports on read; tests pin it to the engine
# rubric. All axes use detail_json, since legacy score columns have different
# meanings.
_REVIEW_AXES: tuple[str, ...] = (
    "scientific_soundness",
    "plausibility",
    "novelty",
    "testability",
    "potential_impact",
    "relevance",
    "safety",
    "clarity",
)


def _axis_scores(raw: Any) -> dict[str, int]:
    """Schema-less responses can invent axes or mistype scores; malformed
    values are discarded rather than coerced.
    """
    if not isinstance(raw, dict):
        return {}
    scores: dict[str, int] = {}
    for axis in _REVIEW_AXES:
        value = raw.get(axis)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if _MIN_AXIS_SCORE <= value <= _MAX_AXIS_SCORE:
            scores[axis] = value
    return scores


def _axis_feedback(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    feedback = {}
    for axis in _REVIEW_AXES:
        text = _clip_detail(raw.get(axis), _MAX_PROSE_CHARS)
        if text:
            feedback[axis] = text
    return feedback


def _bounded_list(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else []
    return [
        text
        for item in items[:_MAX_DETAIL_ITEMS]
        if (text := _clip_detail(item, _MAX_DETAIL_CHARS))
    ]


def _initial_review_detail(rv: dict[str, Any]) -> dict[str, Any]:
    """Structured review parts remain beside workbench prose; reparsing
    flattened critique would lose their meaning.
    """
    detail: dict[str, Any] = {}
    for key, value in (
        ("scores", _axis_scores(rv.get("scores"))),
        (
            "constructive_feedback",
            _clip_detail(rv.get("constructive_feedback"), _MAX_PROSE_CHARS),
        ),
        ("detailed_feedback", _axis_feedback(rv.get("detailed_feedback"))),
        ("already_explored", _bounded_list(rv.get("already_explored"))),
        ("novel_aspects", _bounded_list(rv.get("novel_aspects"))),
    ):
        if value:
            detail[key] = value
    return detail


# Display labels distinguish negative evidence from uncertainty; stored literals
# remain ranking-filter inputs.
_ASSUMPTION_SUPPORT_LABELS: dict[str, str] = {
    "supported": "Plausible",
    "uncertain": "Plausible, but requires careful investigation",
    "likely_false": "Implausible",
}


def _simulation_detail(review: dict[str, Any]) -> dict[str, Any]:
    """Failure points are display-only and may be empty for a holds verdict."""
    raw_points = review.get("failure_points")
    points = [
        clipped
        for point in (raw_points if isinstance(raw_points, list) else [])[:_MAX_DETAIL_ITEMS]
        if (clipped := _clip_detail(point, _MAX_DETAIL_CHARS))
    ]
    decisive_step = _clip_detail(review.get("decisive_step"), _MAX_SHORT_CHARS)
    detail: dict[str, Any] = {}
    if points:
        detail["failure_points"] = points
    if decisive_step:
        detail["decisive_step"] = decisive_step
    return detail


def _verdict_detail(review: dict[str, Any]) -> dict[str, Any]:
    """Go/No-Go display fields are not disposition inputs; gates read the
    engine's verdict and justification.
    """
    go_no_go = _clip_detail(review.get("go_no_go_recommendation"), _MAX_SHORT_CHARS)
    time_to_verdict = _clip_detail(review.get("time_to_verdict"), _MAX_SHORT_CHARS)
    detail: dict[str, Any] = {}
    if go_no_go:
        detail["go_no_go"] = go_no_go
    if time_to_verdict:
        detail["time_to_verdict"] = time_to_verdict
    return detail


_REVIEWS_SUMMARY_PROSE: tuple[str, ...] = ("executive_verdict", "conclusion")
_REVIEWS_SUMMARY_LISTS: tuple[str, ...] = (
    "critical_flaws",
    "addressed_objections",
    "validated_risks",
    "supporting_arguments",
    "alignment_and_novelty",
    "feasibility_assessment",
)


def _reviews_summary_detail(raw: Any) -> dict[str, Any]:
    """Absent or malformed optional summaries mean no structured detail, not
    an empty scaffold.
    """
    if not isinstance(raw, dict):
        return {}
    summary: dict[str, Any] = {
        name: text
        for name in _REVIEWS_SUMMARY_PROSE
        if (text := _clip_detail(raw.get(name), _MAX_PROSE_CHARS))
    }
    summary.update(
        {name: items for name in _REVIEWS_SUMMARY_LISTS if (items := _bounded_list(raw.get(name)))}
    )
    return summary


def _assumption_detail(item: Any) -> dict[str, str] | None:
    """Structured assumptions avoid splitting flattened critique back into
    semantic parts.
    """
    if not isinstance(item, dict):
        return None
    assumption = _clip_detail(item.get("assumption"), _MAX_DETAIL_CHARS)
    if not assumption:
        return None
    support = str(item.get("support") or "").strip()
    label = _ASSUMPTION_SUPPORT_LABELS.get(support, support or "unrated")
    return {
        "assumption": assumption,
        "reasoning": _clip_detail(item.get("reasoning"), _MAX_PROSE_CHARS),
        "support": label,
    }


_MATURE_PROSE_FIELDS: tuple[str, ...] = (
    "correctness",
    "quality_and_novelty",
    "literature_grounding",
    "justification",
    "comparison_with_knowledge_base",
    "goal_requirements_assessment",
    "feasibility_reasoning",
    "impact_assessment",
)


def _mature_prose_fields(review: dict[str, Any]) -> dict[str, str]:
    return {
        name: text
        for name in _MATURE_PROSE_FIELDS
        if (text := _clip_detail(review.get(name), _MAX_PROSE_CHARS))
    }


def _mature_review_detail(review: dict[str, Any]) -> dict[str, Any]:
    """Structured review detail is display-only; disposition uses the
    original engine verdict and justification.
    """
    detail: dict[str, Any] = _verdict_detail(review)
    detail.update(_mature_prose_fields(review))
    if steps := _bounded_list(review.get("feasibility_steps")):
        detail["feasibility_steps"] = steps
    raw = review.get("assumptions")
    assumptions = [
        parsed
        for item in (raw if isinstance(raw, list) else [])[:_MAX_DETAIL_ITEMS]
        if (parsed := _assumption_detail(item))
    ]
    if assumptions:
        detail["assumptions"] = assumptions
    if summary := _reviews_summary_detail(review.get("reviews_summary")):
        detail["reviews_summary"] = summary
    return detail


def _deep_verification_detail(probes: list[dict[str, Any]], verdict: Any) -> dict[str, Any]:
    """Structured probes avoid Markdown collapsing indented
    question/answer/reasoning prose into one paragraph.
    """
    entries: list[dict[str, Any]] = []
    for probe in probes[:_MAX_DETAIL_ITEMS]:
        if not isinstance(probe, dict):
            continue
        question = _clip_detail(probe.get("question"), _MAX_DETAIL_CHARS)
        if not question:
            continue
        entries.append(
            {
                "question": question,
                "answer": _clip_detail(probe.get("answer"), _MAX_PROSE_CHARS),
                "reasoning": _clip_detail(probe.get("reasoning"), _MAX_PROSE_CHARS),
                "fundamental": bool(probe.get("assumption_is_fundamental")),
            }
        )
    if not entries:
        return {}
    return {
        "verdict": str(verdict or "unspecified"),
        "probes": entries,
    }


def _detail_json(detail: dict[str, Any]) -> str | None:
    """Null means no structured detail; an empty object would claim the
    detail existed but was empty.
    """
    return json.dumps(detail) if detail else None


def _review_detail_json(key: str, review: dict[str, Any]) -> str | None:
    if key == "simulation":
        detail = _simulation_detail(review)
    elif key in ("full", "recurrent"):
        detail = _mature_review_detail(review)
    else:
        detail = {}
    return _detail_json(detail)


def _score_or_none(value: Any) -> float | None:
    return float(value or 0) or None


# Scientist reviews retain distinct authorship rather than appearing as
# anonymous agent reviews.
_SCIENTIST_REVIEWER = "scientist"


def _scientist_review_row_survives(source_id: str, conn: sqlite3.Connection) -> bool:
    """When an ID cannot name a surviving row, restoring the review is safer
    than losing scientist input.
    """
    if not source_id.isdigit():
        return False
    return store.review_exists(int(source_id), conn=conn)


def _persist_scientist_review(
    run_id: str, hyp_id: str, rv: dict[str, Any], conn: sqlite3.Connection
) -> bool:
    """Avoid duplicating surviving human reviews; restore those removed by a
    replayed agent-hypothesis cascade from checkpointed provenance.
    """
    feedback = rv.get("detailed_feedback") or {}
    source_id = str(feedback.get("scientist_review_id") or "")
    if not source_id:
        return False
    if _scientist_review_row_survives(source_id, conn):
        return True
    store.add_review(
        NewReview(
            run_id=run_id,
            hypothesis_id=hyp_id,
            reviewer_agent=_SCIENTIST_REVIEWER,
            summary=rv.get("review_summary", ""),
            critique=str(feedback.get("scientist_critique") or ""),
            overall=_score_or_none(rv.get("overall_score", 0)),
            author=str(feedback.get("scientist_author") or ""),
            verdict=str(feedback.get("scientist_verdict") or "") or None,
        ),
        conn=conn,
    )
    return True


def _novelty_review_lines(rv: dict[str, Any]) -> list[str]:

    already = [str(x).strip() for x in rv.get("already_explored") or []]
    already = [x for x in already if x]
    novel = [str(x).strip() for x in rv.get("novel_aspects") or []]
    novel = [x for x in novel if x]
    lines: list[str] = []
    if already:
        lines += ["Aspects already explored:"]
        lines += [f"- {item}" for item in already]
    if novel:
        if lines:
            lines.append("")
        lines += ["Novel Aspects:"]
        lines += [f"- {item}" for item in novel]
    return lines


def _persist_engine_review_rows(
    run_id: str, hyp_id: str, h: dict[str, Any], conn: sqlite3.Connection
) -> None:
    validation = h.get("novelty_validation")
    unknown = isinstance(validation, dict) and validation.get("decision") == "unknown"
    for rv in h.get("reviews") or []:
        if _persist_scientist_review(run_id, hyp_id, rv, conn):
            continue
        if unknown:
            rv = {
                **rv,
                "scores": {k: v for k, v in rv.get("scores", {}).items() if k != "novelty"},
                "detailed_feedback": {
                    k: v for k, v in rv.get("detailed_feedback", {}).items() if k != "novelty"
                },
                "novel_aspects": [],
            }
        scores = rv.get("scores", {})
        critique_lines = [str(rv.get("constructive_feedback") or "")]
        novelty_lines = _novelty_review_lines(rv)
        if novelty_lines:
            critique_lines += ["", *novelty_lines]
        store.add_review(
            NewReview(
                run_id=run_id,
                hypothesis_id=hyp_id,
                reviewer_agent="review",
                summary=rv.get("review_summary", ""),
                critique="\n".join(critique_lines).strip(),
                novelty=_score_or_none(scores.get("novelty", 0)),
                plausibility=_score_or_none(scores.get("scientific_soundness", 0)),
                testability=_score_or_none(scores.get("testability", 0)),
                overall=_score_or_none(rv.get("overall_score", 0)),
                detail_json=_detail_json(_initial_review_detail(rv)),
            ),
            conn=conn,
        )


def _persist_deep_verification_review(
    run_id: str,
    hyp_id: str,
    h: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    probes = h.get("deep_verification_probes") or []
    if not probes:
        return
    summary, critique = format_deep_verification_critique(
        probes, h.get("deep_verification_verdict")
    )
    store.add_review(
        NewReview(
            run_id=run_id,
            hypothesis_id=hyp_id,
            reviewer_agent="deep_verification",
            summary=summary,
            critique=critique,
            detail_json=_detail_json(
                _deep_verification_detail(probes, h.get("deep_verification_verdict"))
            ),
        ),
        conn=conn,
    )


_MATURE_REVIEW_AGENTS: tuple[tuple[str, str, str], ...] = (
    ("full", "full_review", "Full review verdict"),
    ("simulation", "simulation_review", "Simulation review verdict"),
    ("recurrent", "recurrent_review", "Recurrent review verdict"),
)


def _format_mature_critique(key: str, review: dict[str, Any]) -> str:
    """Only schema content reaches prose; retrieval bookkeeping remains
    internal.
    """
    lines: list[str] = []
    if key == "simulation":
        _append_simulation_critique(lines, review)
    else:
        _append_full_critique(lines, review)
    return "\n".join(lines).strip()


def _labeled_lines(pairs: tuple[tuple[str, Any], ...]) -> list[str]:
    lines = []
    for label, value in pairs:
        text = str(value or "").strip()
        if text:
            lines.append(f"{label}: {text}")
    return lines


def _assumption_line(item: dict[str, Any]) -> str | None:
    """Rendered critique is persisted at drain time, so historical label
    wording stays unchanged on read.
    """
    assumption = str(item.get("assumption") or "").strip()
    if not assumption:
        return None
    support = str(item.get("support") or "").strip()
    label = _ASSUMPTION_SUPPORT_LABELS.get(support, support or "unrated")
    line = f"Assumption ({label}): {assumption}"
    reasoning = str(item.get("reasoning") or "").strip()
    if reasoning:
        line += f" — {reasoning}"
    return line


def _append_full_critique(lines: list[str], review: dict[str, Any]) -> None:
    lines += _labeled_lines(
        (
            ("Correctness", review.get("correctness")),
            ("Quality and novelty", review.get("quality_and_novelty")),
            ("Literature grounding", review.get("literature_grounding")),
            ("Justification", review.get("justification")),
        )
    )
    for item in review.get("assumptions") or []:
        if isinstance(item, dict) and (line := _assumption_line(item)):
            lines.append(line)


def _simulation_step_line(index: int, item: dict[str, Any]) -> str | None:
    step = str(item.get("step") or "").strip()
    if not step:
        return None
    plausible = "plausible" if item.get("plausible") else "implausible"
    return f"Step {index} ({plausible}): {step}"


def _simulation_step_lines(review: dict[str, Any]) -> list[str]:
    lines = []
    for index, item in enumerate(review.get("steps") or [], start=1):
        if isinstance(item, dict) and (line := _simulation_step_line(index, item)):
            lines.append(line)
    return lines


def _failure_point_lines(review: dict[str, Any]) -> list[str]:
    lines = []
    for point in review.get("failure_points") or []:
        text = str(point).strip()
        if text:
            lines.append(f"Failure point: {text}")
    return lines


def _append_simulation_critique(lines: list[str], review: dict[str, Any]) -> None:
    model = str(review.get("model") or "").strip()
    if model:
        lines.append(f"Simulated model: {model}")
    lines += _simulation_step_lines(review)
    lines += _failure_point_lines(review)
    lines += _labeled_lines(
        (
            ("Robustness", review.get("robustness")),
            ("Decisive step", review.get("decisive_step")),
        )
    )


def _persist_mature_review_rows(
    run_id: str,
    hyp_id: str,
    h: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    enrichments = h.get("enrichments") or {}
    for key, reviewer_agent, verdict_label in _MATURE_REVIEW_AGENTS:
        review = enrichments.get(key)
        if not isinstance(review, dict):
            continue
        verdict = str(review.get("verdict") or "unspecified")
        store.add_review(
            NewReview(
                run_id=run_id,
                hypothesis_id=hyp_id,
                reviewer_agent=reviewer_agent,
                summary=f"{verdict_label}: {verdict}",
                critique=_format_mature_critique(key, review),
                detail_json=_review_detail_json(key, review),
            ),
            conn=conn,
        )


def _persist_engine_reviews(
    run_id: str,
    hyp_id: str,
    h: dict[str, Any],
    conn: sqlite3.Connection,
) -> None:
    validation = h.get("novelty_validation")
    if isinstance(validation, dict) and validation.get("decision") == "unknown":
        store.add_review(
            NewReview(
                run_id=run_id,
                hypothesis_id=hyp_id,
                reviewer_agent="novelty_validation",
                summary="Prior-art retrieval unknown; no novelty credit",
                critique="Failed sources: " + ", ".join(validation.get("failed_sources") or []),
                verdict="unknown",
                detail_json=json.dumps(validation),
            ),
            conn=conn,
        )
    _persist_engine_review_rows(run_id, hyp_id, h, conn)
    _persist_deep_verification_review(run_id, hyp_id, h, conn)
    _persist_mature_review_rows(run_id, hyp_id, h, conn)


__all__ = [
    "_ASSUMPTION_SUPPORT_LABELS",
    "_CitationSink",
    "_detail_json",
    "_persist_engine_citations",
]
