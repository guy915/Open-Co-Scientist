from __future__ import annotations

import dataclasses
import json
from typing import Any, Final, NamedTuple

from co_scientist.domains.report.markdown.document import (
    _render_about_disclosure,
    _render_data_sources_section,
    _render_knowledge_base_markdown,
    _render_provenance_line,
    _render_research_goal_details,
    _render_summary_section,
    _render_table_of_contents,
    _render_title_and_provider,
)
from co_scientist.domains.report.markdown.hypothesis import (
    _render_hypothesis_entry,
    _render_references_section,
    _render_screened_entry,
    group_by_hypothesis,
    references_by_hypothesis,
)
from co_scientist.domains.report.markdown.overview import (
    _render_main_research_directions_markdown,
    _render_meta_review_overview_markdown,
    _render_meta_review_ranking_markdown,
    research_overview_sections,
)
from co_scientist.domains.report.retrieval import failed_sources_in


def _render_stratification_attributes_markdown(
    attributes: list[dict[str, Any]] | None,
) -> list[str]:
    items = [
        attr
        for attr in attributes or []
        if isinstance(attr, dict) and str(attr.get("name") or "").strip()
    ]
    if not items:
        return []
    lines = ["## Stratification Attributes\n"]
    for attr in items:
        name = str(attr["name"]).strip()
        rubric = str(attr.get("rubric") or "").strip()
        lines.append(f"- **{name}:** {rubric}" if rubric else f"- **{name}**")
    lines.append("")
    return lines


def _critical_criterion_name(criterion: Any) -> str:
    if isinstance(criterion, str):
        return criterion.strip()
    if isinstance(criterion, dict):
        return str(criterion.get("name") or "").strip()
    return ""


def _critical_criterion_description(criterion: Any) -> str:
    if not isinstance(criterion, dict):
        return ""
    return str(criterion.get("description") or "").strip()


def _critical_criteria_entries(
    critical_criteria: list[Any] | None,
) -> list[tuple[str, str]]:
    if not isinstance(critical_criteria, list):
        return []
    return [
        (name, _critical_criterion_description(item))
        for item in critical_criteria
        for name in [_critical_criterion_name(item)]
        if name
    ]


def _render_evaluation_criterion(name: str, description: str) -> list[str]:
    if description:
        return [f"**{name}:** {description}", ""]
    return [f"- {name}"]


def _render_evaluation_criteria_markdown(
    critical_criteria: list[Any] | None,
) -> list[str]:
    entries = _critical_criteria_entries(critical_criteria)
    if not entries:
        return []
    lines = ["## Evaluation Criteria\n"]
    for name, description in entries:
        lines.extend(_render_evaluation_criterion(name, description))
    if lines[-1] != "":
        lines.append("")
    return lines


def _render_review_summary_question(question: Any) -> str:
    """Questions have no legacy bare-string shape, unlike criteria."""
    if not isinstance(question, dict):
        return ""
    name = str(question.get("name") or "").strip()
    text = str(question.get("question") or "").strip()
    if not text:
        return ""
    return f"- **{name}:** {text}" if name else f"- {text}"


def _render_review_summary_criterion(index: int, criterion: Any, name: str) -> list[str]:
    """Legacy bare-name criteria still render without question bullets until
    production reset.
    """
    lines = [f"### {index}. {name}\n"]
    raw_questions = criterion.get("questions") if isinstance(criterion, dict) else None
    questions = raw_questions if isinstance(raw_questions, list) else []
    for question in questions:
        line = _render_review_summary_question(question)
        if line:
            lines.append(line)
    lines.append("")
    return lines


def _render_review_summary_markdown(
    critical_criteria: list[Any] | None,
) -> list[str]:
    if not isinstance(critical_criteria, list):
        return []
    named = [
        (criterion, name)
        for criterion in critical_criteria
        for name in [_critical_criterion_name(criterion)]
        if name
    ]
    if not named:
        return []
    lines = ["## Review Summary\n"]
    for index, (criterion, name) in enumerate(named, start=1):
        lines.extend(_render_review_summary_criterion(index, criterion, name))
    return lines


_MAX_DEBATES: Final = 5


_MAX_TURNS: Final = 5


_MAX_TURN_CHARS: Final = 1200


_MIN_DEBATE_TURNS: Final = 2


class _Debate(NamedTuple):
    idea_1: str
    idea_2: str
    verdict: str
    turns: list[dict[str, Any]]


def _stored_document(match: dict[str, Any]) -> dict[str, Any] | None:
    """Old or malformed transcripts omit the debate rather than publishing a
    fragment.
    """
    raw = match.get("debate_transcript")
    if not raw:
        return None
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _resolve_debate(match: dict[str, Any], titles: dict[str, str]) -> _Debate | None:
    """Recover the judge's canonical idea order from its verdict; deriving order
    from the winner would always show idea 1 winning.
    """
    document = _stored_document(match)
    if document is None:
        return None
    turns = [t for t in document.get("turns") or [] if isinstance(t, dict)]
    winner = titles.get(str(match.get("winner_id") or ""))
    loser = titles.get(str(match.get("loser_id") or ""))
    if not winner or not loser or len(turns) < _MIN_DEBATE_TURNS:
        return None
    verdict = str(document.get("verdict") or "1")
    first, second = (winner, loser) if verdict == "1" else (loser, winner)
    return _Debate(first, second, verdict, turns)


def _select_debates(matches: list[dict[str, Any]], titles: dict[str, str]) -> list[_Debate]:
    """Turn count prioritizes contested comparisons; stable sorting preserves
    tournament order for equal depths.
    """
    debates = [
        debate for match in matches if (debate := _resolve_debate(match, titles)) is not None
    ]
    debates.sort(key=lambda debate: len(debate.turns), reverse=True)
    return debates[:_MAX_DEBATES]


def _turn_text(turn: dict[str, Any]) -> str:
    text = " ".join(str(turn.get("text") or "").split())
    if len(text) <= _MAX_TURN_CHARS:
        return text
    return text[:_MAX_TURN_CHARS].rstrip() + " …"


def _numbering_note(turn: dict[str, Any]) -> str:
    first = str(turn.get("first") or "")
    if first not in ("1", "2"):
        return ""
    return f'; this turn\'s "Hypothesis 1" is idea {first}'


def _render_turn(index: int, turn: dict[str, Any]) -> str:
    """Use the section's numbering for favored ideas; preserve the transcript's
    own numbering as a separate note.
    """
    favored = "2" if str(turn.get("favored") or "1") == "2" else "1"
    return f"**Turn {index} (favors idea {favored}{_numbering_note(turn)}):** {_turn_text(turn)}"


def _render_debate(number: int, debate: _Debate) -> list[str]:
    lines = [
        f"### Debate {number}: 1. {debate.idea_1} vs 2. {debate.idea_2}",
        "",
    ]
    for index, turn in enumerate(debate.turns[:_MAX_TURNS], 1):
        lines += [_render_turn(index, turn), ""]
    lines += [f"Better idea: {debate.verdict}", ""]
    return lines


def _render_tournament_debates_markdown(
    matches: list[dict[str, Any]],
    hypothesis_title_by_id: dict[str, str] | None,
) -> list[str]:
    debates = _select_debates(matches, hypothesis_title_by_id or {})
    if not debates:
        return []
    lines = [
        "## Tournament debates",
        "",
        "_Each pairing is judged as a simulated expert debate: the judge"
        " re-examines the exchange each turn, with the two ideas presented"
        " in alternating order, before the closing verdict. Because that"
        " order alternates, each turn's header names which idea that"
        ' turn\'s own text calls "Hypothesis 1"; "favors idea N" always'
        " uses this section's numbering._",
        "",
    ]
    for number, debate in enumerate(debates, 1):
        lines += _render_debate(number, debate)
    return lines


# Normal app runs do not corpus-check model novelty; disclose that novelty
# judgments remain directional.
_NOVELTY_DISCLOSURE = (
    "_Novelty above reflects the reviewing model's own judgment, not a"
    " search of the published literature. Treat any claim that an idea is"
    " original, unprecedented, or unexplored as directional, not verified._"
)


def _render_novelty_disclosure(
    top_hypotheses: list[dict[str, Any]],
) -> list[str]:
    if not top_hypotheses or any(hyp.get("novelty_validation") for hyp in top_hypotheses):
        return []
    return [_NOVELTY_DISCLOSURE, ""]


def _render_top_hypotheses_markdown(
    top_hypotheses: list[dict[str, Any]],
    claim_evidence: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> list[str]:
    edges_by_hypothesis = group_by_hypothesis(claim_evidence)
    refs_by_hypothesis = references_by_hypothesis(citations, evidence)
    reviews_by_hypothesis = group_by_hypothesis(reviews)
    lines: list[str] = ["## Top hypotheses", ""]
    lines += _render_novelty_disclosure(top_hypotheses)
    for i, hyp in enumerate(top_hypotheses, 1):
        hyp_id = str(hyp.get("id") or "")
        lines += _render_hypothesis_entry(
            i,
            hyp,
            edges_by_hypothesis.get(hyp_id, []),
            refs_by_hypothesis.get(hyp_id, []),
            reviews_by_hypothesis.get(hyp_id, []),
        )
    return lines


def _render_screened_hypotheses_markdown(
    screened: list[dict[str, Any]], first_number: int
) -> list[str]:
    """Every idea that passed the screen stays in the report; numbering
    continues the featured list so each idea keeps one heading."""
    if not screened:
        return []
    lines: list[str] = ["## Screened ideas", ""]
    for offset, hyp in enumerate(screened):
        lines += _render_screened_entry(first_number + offset, hyp)
    return lines


def _render_citation_audit(
    citation_summary: dict[str, int] | None,
) -> list[str]:
    if not citation_summary:
        return []
    lines = ["## Citation audit"]
    lines.extend(f"- {state}: {count}" for state, count in citation_summary.items())
    lines.append("")
    return lines


@dataclasses.dataclass(frozen=True)
class ReportMarkdownInputs:
    research_goal: str
    provider: str
    top_hypotheses: list[dict[str, Any]]
    goal_restatement: str | None = None
    screened_hypotheses: list[dict[str, Any]] | None = None
    meta_review: dict[str, Any] | None = None
    citation_summary: dict[str, int] | None = None
    research_overview: dict[str, Any] | None = None
    knowledge_base: list[dict[str, Any]] | None = None
    setup: dict[str, Any] | None = None
    # Synthesized stratification axes differ from scientist setup attributes.
    attributes: list[dict[str, Any]] | None = None
    # Synthesized reviewer guidance differs from scientist criteria; legacy
    # names and richer objects both remain readable.
    critical_criteria: list[Any] | None = None
    prepared_at: float | None = None
    summary: str | None = None
    claim_evidence: list[dict[str, Any]] | None = None
    skills_used: dict[str, int] | None = None
    retrieval_calls: list[dict[str, Any]] | None = None
    # Legacy reports without citation data must never invent references.
    citations: list[dict[str, Any]] | None = None
    evidence: list[dict[str, Any]] | None = None
    reviews: list[dict[str, Any]] | None = None
    hypothesis_title_by_id: dict[str, str] | None = None
    matches: list[dict[str, Any]] | None = None


def _report_sections(inputs: ReportMarkdownInputs) -> list[list[str]]:
    """The contents and body share one section skeleton so omitted sections
    cannot leave dead navigation entries.
    """
    top_hypotheses = _render_top_hypotheses_markdown(
        inputs.top_hypotheses,
        inputs.claim_evidence or [],
        inputs.citations or [],
        inputs.evidence or [],
        inputs.reviews or [],
    )
    failures = failed_sources_in(inputs.reviews or [], inputs.retrieval_calls)
    if failures:
        top_hypotheses[2:2] = [
            "_Prior-art retrieval is unknown for failed sources: "
            + ", ".join(failures)
            + ". These searches receive no novelty credit._",
            "",
        ]
    if inputs.goal_restatement:
        top_hypotheses[2:2] = [inputs.goal_restatement, ""]
    return [
        _render_research_goal_details(inputs.research_goal, inputs.setup),
        _render_provenance_line(inputs.prepared_at),
        _render_summary_section(inputs.summary),
        _render_evaluation_criteria_markdown(inputs.critical_criteria),
        _render_stratification_attributes_markdown(inputs.attributes),
        _render_meta_review_overview_markdown(inputs.meta_review or {}),
        *research_overview_sections(inputs.research_overview or {}, inputs.hypothesis_title_by_id),
        _render_review_summary_markdown(inputs.critical_criteria),
        _render_main_research_directions_markdown(inputs.meta_review or {}),
        top_hypotheses,
        _render_screened_hypotheses_markdown(
            inputs.screened_hypotheses or [], len(inputs.top_hypotheses) + 1
        ),
        _render_meta_review_ranking_markdown(inputs.meta_review or {}),
        _render_tournament_debates_markdown(inputs.matches or [], inputs.hypothesis_title_by_id),
        _render_knowledge_base_markdown(inputs.knowledge_base or []),
        _render_data_sources_section(inputs.skills_used or {}, inputs.retrieval_calls or []),
        _render_citation_audit(inputs.citation_summary),
        _render_references_section(inputs.evidence or []),
    ]


def render_report_markdown(inputs: ReportMarkdownInputs) -> str:
    title_lines = _render_title_and_provider(inputs.research_goal, inputs.provider)
    about_lines = _render_about_disclosure()
    sections = _report_sections(inputs)
    lines = title_lines + about_lines + _render_table_of_contents(sections)
    for section in sections:
        lines += section
    return "\n".join(lines)
