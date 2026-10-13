from __future__ import annotations

import json
import re
from typing import Any

from co_scientist.domains.report.markdown.document import _ABOUT_DISCLOSURE, _SYSTEM_NAME
from co_scientist.domains.research_state.claims.gate import ClaimEdge
from co_scientist.domains.research_state.models import HypothesisOrigin
from co_scientist.domains.research_state.text_utils import hypothesis_statement, hypothesis_title
from co_scientist.platform.retrieval.citations import (
    CitationMetadata,
    DateState,
    SourceType,
    classify_date,
    classify_source_type,
)
from co_scientist.platform.retrieval.evidence.article_support import present_value

# Anchor the drain citation prefix so unrelated seeded claims remain unresolved
# rather than mismatched.
_CLAIM_KEY_PREFIX = re.compile(r"^\[(C\d+)\]")

_ET_AL_SUFFIX = re.compile(r"\s+et\s+al\.?\s*$", re.IGNORECASE)


def _citation_key(claim: str) -> str | None:
    match = _CLAIM_KEY_PREFIX.match(claim)
    return match.group(1) if match else None


def _reference_label(evidence: dict[str, Any]) -> str:
    title = str(evidence.get("title") or "").strip()
    authors = evidence.get("authors") or []
    year = evidence.get("year")
    # Strip curated et al. credits before surname parsing, or al. becomes the
    # surname.
    first_author_text = _ET_AL_SUFFIX.sub("", str(authors[0]).strip()) if authors else ""
    first_author = first_author_text.split()[-1] if first_author_text else ""
    if first_author and year:
        prefix = f"{first_author} et al., {year}"
        return f"{prefix} — {title}" if title else prefix
    return title or "Untitled source"


def _render_reference_line(key: str, evidence: dict[str, Any]) -> str:
    label = _reference_label(evidence)
    url = str(evidence.get("url") or "")
    text = f"[{label}]({url})" if url else label
    return f"- **[{key}]** {text}"


def references_by_hypothesis(
    citations: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    evidence_by_id = {str(row.get("id")): row for row in evidence}
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for citation in citations:
        key = _citation_key(str(citation.get("claim") or ""))
        source = evidence_by_id.get(str(citation.get("evidence_id") or ""))
        if key is None or source is None:
            continue
        hyp_id = str(citation.get("hypothesis_id") or "")
        grouped.setdefault(hyp_id, []).append((key, source))
    for entries in grouped.values():
        entries.sort(key=lambda pair: int(pair[0][1:]))
    return grouped


def _render_references_markdown(
    entries: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    if not entries:
        return []
    lines = ["#### References", ""]
    lines.extend(_render_reference_line(key, source) for key, source in entries)
    lines.append("")
    return lines


def _identifier(evidence: dict[str, Any], field: str) -> str:
    """Runs recorded before the PubMed parser fix hold a missing-value
    sentinel here, which is one identifier every unidentified paper shares.
    """
    return str(present_value(evidence.get(field)) or "").strip()


def _normalized_title(title: str) -> str:
    return " ".join(title.strip().lower().split())


def _evidence_identity(evidence: dict[str, Any]) -> str:
    """Unidentifiable synthetic rows keep distinct ids rather than being merged
    with equally unidentifiable rows.
    """
    doi = _identifier(evidence, "doi").lower()
    if doi:
        return f"doi:{doi}"
    pmid = _identifier(evidence, "pmid")
    if pmid:
        return f"pmid:{pmid}"
    url = _identifier(evidence, "url")
    if url:
        return f"url:{url}"
    title = _normalized_title(str(evidence.get("title") or ""))
    if title:
        return f"title:{title}"
    return f"id:{evidence.get('id')}"


def _dedupe_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First-seen wins in persisted creation order, preserving the earliest copy
    of a repeatedly retrieved source.
    """
    by_identity: dict[str, dict[str, Any]] = {}
    for row in evidence:
        by_identity.setdefault(_evidence_identity(row), row)
    return list(by_identity.values())


def _identifier_suffix(evidence: dict[str, Any]) -> str:
    """Display the same authoritative identifier used for deduplication."""
    doi = _identifier(evidence, "doi")
    if doi:
        return f" (DOI: {doi})"
    pmid = _identifier(evidence, "pmid")
    if pmid:
        return f" (PMID: {pmid})"
    return ""


def _retracted_suffix(evidence: dict[str, Any]) -> str:
    """A plain reference would hide the known retraction from the reader."""
    return " (retracted)" if evidence.get("retracted") else ""


# Label non-reviewed source types without guessing unknown classifications.
_SOURCE_TYPE_WORDS = {
    SourceType.PREPRINT: "preprint",
    SourceType.DATABASE: "database record",
    SourceType.WEB: "web page",
    SourceType.DOCUMENT: "attached document",
}


def _row_metadata(evidence: dict[str, Any]) -> CitationMetadata:
    return CitationMetadata(
        url=str(evidence.get("url") or ""),
        doi=str(evidence.get("doi") or ""),
        pmid=str(evidence.get("pmid") or ""),
        retracted=bool(evidence.get("retracted")),
        source=str(evidence.get("source") or ""),
        year=evidence.get("year"),
    )


def _row_source_type(evidence: dict[str, Any]) -> SourceType:
    """Prefer persisted classification: the drain saw publisher metadata this
    evidence row no longer carries.
    """
    stored = str(evidence.get("source_type") or "")
    if stored in {member.value for member in SourceType}:
        return SourceType(stored)
    return classify_source_type(_row_metadata(evidence))


def _source_type_suffix(source_type: SourceType) -> str:
    word = _SOURCE_TYPE_WORDS.get(source_type)
    return f" ({word})" if word else ""


def _date_suffix(evidence: dict[str, Any], source_type: SourceType) -> str:
    state = classify_date(_row_metadata(evidence))
    if state is DateState.MISSING and source_type.is_publication:
        return " (no date)"
    if state is DateState.IMPLAUSIBLE:
        return " (date not verifiable)"
    return ""


def _render_reference_entry(evidence: dict[str, Any]) -> str:
    source_type = _row_source_type(evidence)
    label = (
        _reference_label(evidence)
        + _identifier_suffix(evidence)
        + _source_type_suffix(source_type)
        + _date_suffix(evidence, source_type)
        + _retracted_suffix(evidence)
    )
    url = str(evidence.get("url") or "")
    text = f"[{label}]({url})" if url else label
    return f"- {text}"


def _render_references_section(evidence: list[dict[str, Any]]) -> list[str]:
    deduped = _dedupe_evidence(evidence)
    if not deduped:
        return []
    ordered = sorted(deduped, key=lambda row: _reference_label(row).casefold())
    lines = ["", "## References", ""]
    lines.extend(_render_reference_entry(row) for row in ordered)
    lines.append("")
    return lines


# Bound related sources and excerpts so a heavily cited idea cannot turn each
# axis into a bibliography.
_MAX_RELATED_ARTICLES = 6

_MAX_ABSTRACT_CHARS = 400

Reference = tuple[str, dict[str, Any]]


def _abstract_excerpt(source: dict[str, Any]) -> str:
    text = " ".join(str(source.get("abstract") or "").split())
    if len(text) <= _MAX_ABSTRACT_CHARS:
        return text
    return text[:_MAX_ABSTRACT_CHARS].rstrip() + "..."


def _related_articles(references: list[Reference], with_abstracts: bool) -> list[str]:
    entries = references[:_MAX_RELATED_ARTICLES]
    if not entries:
        return []
    label = (
        "**Related Article Abstracts**" if with_abstracts else "**Related Article Abstract Titles**"
    )
    lines = [label, ""]
    for key, source in entries:
        line = f"- **[{key}]** {_reference_label(source)}"
        if with_abstracts and (excerpt := _abstract_excerpt(source)):
            line += f": {excerpt}"
        lines.append(line)
    return [*lines, ""]


def _prose(label: str, value: Any) -> list[str]:
    text = str(value or "").strip()
    return [f"**{label}**", "", text, ""] if text else []


def _steps(label: str, items: Any) -> list[str]:
    entries = [
        text for item in (items if isinstance(items, list) else []) if (text := str(item).strip())
    ]
    if not entries:
        return []
    numbered = [f"{n}. {text}" for n, text in enumerate(entries, start=1)]
    return [f"**{label}**", "", *numbered, ""]


def _feasibility_extras(initial: dict[str, Any], mature: dict[str, Any]) -> list[str]:
    del initial
    return [
        *_steps("Steps to Test the Idea", mature.get("feasibility_steps")),
        *_prose("Reasoning about Feasibility", mature.get("feasibility_reasoning")),
    ]


def _impact_extras(initial: dict[str, Any], mature: dict[str, Any]) -> list[str]:
    """Assumptions and improvements already appear under Correctness; repeating
    them adds no evidence.
    """
    del initial
    return _prose("Overall Impact Potential", mature.get("impact_assessment"))


def related_article_abstracts(references: list[Reference]) -> list[str]:
    """Correctness weighs the evidence itself, so it carries abstracts rather
    than only pointers.
    """
    return _related_articles(references, with_abstracts=True)


def related_article_titles(references: list[Reference]) -> list[str]:
    return _related_articles(references, with_abstracts=False)


# Map only equivalent review axes; preserve distinct axes rather than
# misdescribe them with borrowed labels.
_AXIS_SECTIONS: tuple[tuple[str, str], ...] = (
    ("scientific_soundness", "Correctness"),
    ("plausibility", "Plausibility"),
    ("novelty", "Novelty"),
    ("testability", "Feasibility"),
    ("potential_impact", "Impact potential"),
    ("relevance", "Relevance"),
    ("safety", "Safety"),
    ("clarity", "Clarity"),
)

_REVIEWS_SUMMARY_SECTIONS: tuple[tuple[str, str], ...] = (
    ("executive_verdict", "1. Executive Verdict"),
    ("critical_flaws", "2. Critical Flaws"),
    ("addressed_objections", "3. Addressed Objections"),
    ("validated_risks", "4. Validated Risks & Limitations"),
    ("supporting_arguments", "5. Supporting Arguments & Evidence (Motivation)"),
    ("alignment_and_novelty", "6. Alignment & Novelty"),
    ("feasibility_assessment", "7. Feasibility Assessment (Go/No-Go Decision)"),
    ("conclusion", "8. Conclusion"),
)


def _latest_detail(reviews: list[dict[str, Any]], reviewer_agent: str) -> dict[str, Any]:
    for row in reversed(reviews):
        if row.get("reviewer_agent") != reviewer_agent:
            continue
        try:
            parsed = json.loads(row.get("detail_json") or "null")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _mature_detail(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Recurrent review supersedes full review, matching the displayed Go/No-Go
    assessment.
    """
    return _latest_detail(reviews, "recurrent_review") or _latest_detail(reviews, "full_review")


def _bullets(label: str, items: Any) -> list[str]:
    entries = [
        text for item in (items if isinstance(items, list) else []) if (text := str(item).strip())
    ]
    if not entries:
        return []
    return [label, *[f"- {entry}" for entry in entries], ""]


def _assumption_lines(mature: dict[str, Any]) -> list[str]:
    raw = mature.get("assumptions")
    lines: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        entry = f"- **{item.get('support')}:** {item.get('assumption')}"
        if reasoning := str(item.get("reasoning") or "").strip():
            entry += f" — {reasoning}"
        lines.append(entry)
    return ["**Detailed Assumptions**", "", *lines, ""] if lines else []


def _correctness_extras(initial: dict[str, Any], mature: dict[str, Any]) -> list[str]:
    return [
        *_assumption_lines(mature),
        *_prose(
            "Comparison with Knowledge Base",
            mature.get("comparison_with_knowledge_base"),
        ),
        *_prose("Reasoning about Correctness", mature.get("correctness")),
        *_prose("Strength of Evidence", mature.get("literature_grounding")),
        *_prose("Suggested Improvements", initial.get("constructive_feedback")),
        *_prose(
            "Goal Requirement Assessment",
            mature.get("goal_requirements_assessment"),
        ),
        *_prose("Final Reasoning and Recommendation", mature.get("justification")),
    ]


def _novelty_extras(initial: dict[str, Any], mature: dict[str, Any]) -> list[str]:
    return [
        *_prose("Reasoning about Novelty", mature.get("quality_and_novelty")),
        *_bullets("Aspects already explored:", initial.get("already_explored")),
        *_bullets("Novel Aspects:", initial.get("novel_aspects")),
    ]


_AXIS_EXTRAS = {
    "scientific_soundness": _correctness_extras,
    "novelty": _novelty_extras,
    "testability": _feasibility_extras,
    "potential_impact": _impact_extras,
}

# Use persisted citations, never model-generated article lists; repeat abstracts
# only where the evidence itself is weighed.
_AXIS_ARTICLES = {
    "scientific_soundness": related_article_abstracts,
    "novelty": related_article_titles,
    "testability": related_article_titles,
    "potential_impact": related_article_titles,
}


def _axis_findings(axis: str, initial: dict[str, Any], mature: dict[str, Any]) -> list[str]:
    feedback = initial.get("detailed_feedback") or {}
    extras = _AXIS_EXTRAS.get(axis)
    text = str(feedback.get(axis) or "").strip()
    return [
        *([text, ""] if text else []),
        *(extras(initial, mature) if extras else []),
    ]


def _axis_section(
    axis: str,
    label: str,
    initial: dict[str, Any],
    mature: dict[str, Any],
    references: list[Reference],
) -> list[str]:
    """Article lists contextualize an actual review verdict; an unjudged axis
    must not become a heading over a bibliography.
    """
    findings = _axis_findings(axis, initial, mature)
    score = (initial.get("scores") or {}).get(axis)
    if not findings and score is None:
        return []
    articles = _AXIS_ARTICLES.get(axis)
    lines = [f"##### {label}", ""]
    lines += articles(references) if articles else []
    lines += findings
    if score is not None:
        lines += [f"**Answer: {score}**", ""]
    return lines


def _render_hypothesis_reviews(
    reviews: list[dict[str, Any]],
    references: list[Reference] | None = None,
) -> list[str]:
    initial = _latest_detail(reviews, "review")
    mature = _mature_detail(reviews)
    cited = list(references or [])
    body: list[str] = []
    for axis, label in _AXIS_SECTIONS:
        body += _axis_section(axis, label, initial, mature, cited)
    if not body:
        return []
    return ["#### Appendix:", "", "**All reviews:**", "", *body]


def _reviews_summary_section(key: str, label: str, value: Any) -> list[str]:
    body = (
        [str(value).strip(), ""]
        if isinstance(value, str)
        else [f"- {text}" for item in value if (text := str(item).strip())]
    )
    if not [line for line in body if line]:
        return []
    trailer = [] if isinstance(value, str) else [""]
    return [f"##### {label}", "", *body, *trailer]


def _render_reviews_summary(reviews: list[dict[str, Any]]) -> list[str]:
    summary = _mature_detail(reviews).get("reviews_summary")
    if not isinstance(summary, dict):
        return []
    body: list[str] = []
    for key, label in _REVIEWS_SUMMARY_SECTIONS:
        value = summary.get(key)
        if isinstance(value, (str, list)):
            body += _reviews_summary_section(key, label, value)
    return ["#### Reviews summary", "", *body] if body else []


def _probe_lines(index: int, probe: dict[str, Any]) -> list[str]:
    flag = "fundamental" if probe.get("fundamental") else "non-fundamental"
    lines = [f"**Probe {index} ({flag} assumption)**", ""]
    for label, key in (
        ("Question", "question"),
        ("Answer", "answer"),
        ("Reasoning", "reasoning"),
    ):
        if text := str(probe.get(key) or "").strip():
            lines += [f"{label}: {text}", ""]
    return lines


_CRITIQUE_PARTS: tuple[str, ...] = ("critical_flaws", "validated_risks")


def _critique_entries(summary: dict[str, Any]) -> list[str]:
    entries: list[str] = []
    for key in _CRITIQUE_PARTS:
        value = summary.get(key)
        if isinstance(value, str):
            if text := value.strip():
                entries.append(text)
        elif isinstance(value, list):
            entries += [t for item in value if (t := str(item).strip())]
    return entries


def _render_critiques_rollup(reviews: list[dict[str, Any]]) -> list[str]:
    summary = _mature_detail(reviews).get("reviews_summary")
    if not isinstance(summary, dict):
        return []
    entries = _critique_entries(summary)
    if not entries:
        return []
    return [
        "#### Critiques",
        "",
        "Here's a summary of the negative critiques from the reviews:",
        "",
        *[f"- {entry}" for entry in entries],
        "",
    ]


def _render_deep_verification(reviews: list[dict[str, Any]]) -> list[str]:
    detail = _latest_detail(reviews, "deep_verification")
    raw = detail.get("probes")
    probes = [item for item in (raw or []) if isinstance(item, dict)]
    if not probes:
        return []
    lines = ["#### Deep verification", ""]
    if verdict := str(detail.get("verdict") or "").strip():
        lines += [f"**Verdict:** {verdict}", ""]
    for index, probe in enumerate(probes, start=1):
        lines += _probe_lines(index, probe)
    return lines


# Research-purpose cautions apply even without evidence or review fields.
_HYPOTHESIS_DISCLAIMER = _ABOUT_DISCLOSURE

# Matches the engine SCIENTIST_REVIEWER value persisted in app review rows.
_SCIENTIST_REVIEWER = "scientist"


def _render_evidence_span(span: dict[str, Any], relation: str) -> str:
    quote = " ".join(str(span.get("quote") or "").split())
    source_title = str(
        span.get("source_title")
        or span.get("source")
        or span.get("evidence_id")
        or "Evidence passage"
    )
    url = str(span.get("url") or "")
    source = f"[{source_title}]({url})" if url else source_title
    return f"  - {relation} span — {source}: “{quote}”"


_ASSESSMENT_METHODS = {
    "legacy_unknown": "not recorded",
    "no_evidence": "no candidate evidence",
    "deterministic_lexical": "lexical comparison",
    "model_primary": "model judgment",
    "lexical_founded": "model judgment with a lexical contradiction check",
    "contradiction_guard_rejected": ("contradiction rejected by provenance checks"),
    "model_opposition_verified": ("separate model opposition check (not scientific validation)"),
    "model_opposition_unconfirmed": ("model opposition check did not confirm contradiction"),
}


def _render_claim_evidence(edges: list[dict[str, Any]]) -> list[str]:
    if not edges:
        return []
    lines = ["**Claim evidence:**", ""]
    for edge in edges:
        typed = ClaimEdge.from_row(edge)
        lines.append(f"- **{typed.status} · {typed.role}** — {typed.claim}")
        method = _ASSESSMENT_METHODS.get(str(edge.get("verification_method")), "not recorded")
        lines.append(f"  Assessment method: {method}.")
        for span in edge.get("supporting") or []:
            lines.append(_render_evidence_span(span, "Supporting"))
        for span in edge.get("contradicting") or []:
            lines.append(_render_evidence_span(span, "Contradicting"))
    lines.append("")
    return lines


def _render_hypothesis_scene_setting(hyp: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for label, value in (
        ("Introduction", hyp.get("introduction")),
        ("Recent findings and related research", hyp.get("recent_findings")),
    ):
        if value:
            lines += [f"#### {label}", "", str(value), ""]
    return lines


def _render_hypothesis_mechanism(hyp: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for label, value in (
        ("**Mechanism:**", hyp.get("mechanism")),
        ("**Predicted effect:**", hyp.get("expected_effect")),
    ):
        if value:
            lines += [f"{label} {value}", ""]
    return lines


def _render_hypothesis_experiment(hyp: dict[str, Any]) -> list[str]:
    """Preserve engine-formatted experiment markdown verbatim; legacy plain
    prose remains readable.
    """
    experiment = hyp.get("experimental_context")
    if not experiment:
        return []
    return ["#### Steps to test the idea", "", str(experiment), ""]


def _render_hypothesis_safety(hyp: dict[str, Any]) -> list[str]:
    """Proposer pharmacological safety differs from reviewer dual-use and
    ethical concerns.
    """
    safety_and_toxicity = hyp.get("safety_and_toxicity")
    if not safety_and_toxicity:
        return []
    return ["#### Safety and toxicity", "", str(safety_and_toxicity), ""]


def group_by_hypothesis(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("hypothesis_id") or ""), []).append(row)
    return grouped


def _is_unreviewed_scientist_admission(hyp: dict[str, Any], reviews: list[dict[str, Any]]) -> bool:
    """A scientist idea admitted at the request cap may publish before its owed
    review; label it unreviewed. A scientist's own review is not peer review.
    """
    if hyp.get("created_by_agent") != HypothesisOrigin.SCIENTIST_MANUAL.value:
        return False
    return not any(review.get("reviewer_agent") != _SCIENTIST_REVIEWER for review in reviews)


def _render_scientist_admission_notice(
    hyp: dict[str, Any], reviews: list[dict[str, Any]]
) -> list[str]:
    """Show scientist provenance before the idea's claims."""
    if not _is_unreviewed_scientist_admission(hyp, reviews):
        return []
    return [
        "**Scientist-contributed — not yet reviewed:** this idea was added "
        "by a scientist and reached the report before the automated review "
        "agents assessed it.",
        "",
    ]


def _review_detail(reviews: list[dict[str, Any]], reviewer_agent: str) -> dict[str, Any]:
    """Legacy absent or malformed structured reviews degrade to empty content
    rather than preventing report reads.
    """
    for row in reviews:
        if row.get("reviewer_agent") != reviewer_agent:
            continue
        raw = row.get("detail_json")
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _render_hypothesis_simulation_review(
    reviews: list[dict[str, Any]],
) -> list[str]:
    """Failure points have no separate name field; ordinal labels avoid
    inventing names for them.
    """
    detail = _review_detail(reviews, "simulation_review")
    raw_points = detail.get("failure_points")
    points = [
        text
        for point in (raw_points if isinstance(raw_points, list) else [])
        if (text := str(point).strip())
    ]
    decisive_step = str(detail.get("decisive_step") or "").strip()
    if not points and not decisive_step:
        return []
    lines = ["#### Simulation review", ""]
    for idx, text in enumerate(points, start=1):
        lines.append(f"{idx}. **Failure point:** {text}")
    if points:
        lines.append("")
    if decisive_step:
        lines += [f"**Decisive step:** {decisive_step}", ""]
    return lines


def _render_hypothesis_verdict(reviews: list[dict[str, Any]]) -> list[str]:
    """Go/No-Go and timeframe are display recommendations, separate from the
    review-disposition gate. A recurrent review supersedes the earlier full
    review.
    """
    detail = _review_detail(reviews, "recurrent_review") or _review_detail(reviews, "full_review")
    go_no_go = str(detail.get("go_no_go") or "").strip()
    time_to_verdict = str(detail.get("time_to_verdict") or "").strip()
    lines: list[str] = []
    if go_no_go:
        lines += [f"**Verdict:** {go_no_go}", ""]
    if time_to_verdict:
        lines += [f"**Time to Verdict:** {time_to_verdict}", ""]
    return lines


def _render_hypothesis_review_surface(
    reviews: list[dict[str, Any]],
    references: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    lines: list[str] = []
    lines += _render_reviews_summary(reviews)
    lines += _render_hypothesis_verdict(reviews)
    lines += _render_hypothesis_reviews(reviews, references)
    lines += _render_hypothesis_simulation_review(reviews)
    lines += _render_deep_verification(reviews)
    lines += _render_critiques_rollup(reviews)
    return lines


# The paired benchmark (`evaluations/_paired_db.py`) counts entries carrying
# this exact phrase as screened rather than featured.
SCREENED_NOTE = (
    "_Screened, not deep-verified: this idea passed the safety screen and a"
    " screening review but was not among the finalists, so it had no full"
    " review, simulation, deep verification or claim check._"
)


def _render_screened_entry(i: int, hyp: dict[str, Any]) -> list[str]:
    title = hypothesis_title(hyp)
    lines = [
        f"### {i}. **{_SYSTEM_NAME} - {title}**  _Elo: {hyp.get('elo_rating', '')}_",
        SCREENED_NOTE,
        "",
    ]
    if statement := hypothesis_statement(hyp):
        lines += [f"**Proposed hypothesis:** {statement}", ""]
    return lines + _render_hypothesis_mechanism(hyp)


def _render_hypothesis_entry(
    i: int,
    hyp: dict[str, Any],
    edges: list[dict[str, Any]],
    references: list[tuple[str, dict[str, Any]]],
    reviews: list[dict[str, Any]],
) -> list[str]:
    """Keep this heading beneath Top hypotheses; use shared title/statement
    formatters across report surfaces.
    """
    title = hypothesis_title(hyp)
    lines = [
        f"### {i}. **{_SYSTEM_NAME} - {title}**  _Elo: {hyp.get('elo_rating', '')}_",
        _HYPOTHESIS_DISCLAIMER,
        "",
    ]
    lines += _render_scientist_admission_notice(hyp, reviews)
    lines += _render_hypothesis_scene_setting(hyp)
    if statement := hypothesis_statement(hyp):
        lines += [f"**Proposed hypothesis:** {statement}", ""]
    lines += _render_hypothesis_mechanism(hyp)
    lines += _render_hypothesis_experiment(hyp)  # R14-20, right after Mechanism
    lines += _render_references_markdown(references)
    lines += _render_hypothesis_safety(hyp)
    lines += _render_hypothesis_review_surface(reviews, references)
    lines += _render_claim_evidence(edges)
    return lines
