from __future__ import annotations

import json
from typing import Any, NamedTuple


def _readable_text(value: Any) -> str:
    if isinstance(value, str):
        return _readable_from_string(value)
    if isinstance(value, list):
        return _join_readable(value, " ")
    if isinstance(value, dict):
        return _join_readable(list(value.values()), " - ")
    return "" if value is None else str(value)


def _readable_from_string(value: str) -> str:
    trimmed = value.strip()
    if not _is_json_like(trimmed):
        return value
    try:
        return _readable_text(json.loads(trimmed))
    except (ValueError, TypeError):
        return value


def _is_json_like(text: str) -> bool:
    return (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    )


def _join_readable(values: list[Any], separator: str) -> str:
    return separator.join(text for text in (_readable_text(item) for item in values) if text)


def _readable_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return _list_from_string(value)
    if isinstance(value, list):
        return [text for text in map(_readable_text, value) if text]
    if isinstance(value, dict):
        text = _readable_text(value)
        return [text] if text else []
    return []


def _list_from_string(value: str) -> list[str]:
    trimmed = value.strip()
    if not trimmed:
        return []
    if not _is_json_like(trimmed):
        return [trimmed]
    try:
        return _readable_text_list(json.loads(trimmed))
    except (ValueError, TypeError):
        return [trimmed]


def _render_contact_evidence_line(contact: dict[str, Any]) -> list[str]:
    title = _readable_text(contact.get("source_title"))
    url = contact.get("source_url")
    if not title:
        return []
    source = f"[{title}]({url})" if url else title
    return [f"**Supporting article:** {source}\n"]


def _render_contact_entry(
    contact: dict[str, Any],
    *,
    heading: str = "###",
    show_direction: bool = True,
) -> list[str]:
    if not isinstance(contact, dict) or not contact.get("name"):
        return []
    lines = [f"{heading} {_readable_text(contact['name'])}\n"]
    if show_direction:
        direction = _readable_text(contact.get("research_direction"))
        if direction:
            lines.append(f"**Research direction:** {direction}\n")
    lines += _render_contact_body(contact)
    lines += _render_contact_evidence_line(contact)
    return lines


def _render_contact_body(contact: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    expertise = _readable_text(contact.get("expertise"))
    if expertise:
        lines.append(f"**Relevant expertise:** {expertise}\n")
    justification = _readable_text(contact.get("justification"))
    if justification:
        lines.append(f"**Justification:** {justification}\n")
    return lines


def _direction_key(direction: Any) -> str:
    return _readable_text(direction).casefold()


def _render_group_example_titles(
    example_hypothesis_ids: Any, hypothesis_title_by_id: dict[str, str]
) -> list[str]:
    ids = example_hypothesis_ids if isinstance(example_hypothesis_ids, list) else []
    titles = [
        hypothesis_title_by_id[hid]
        for hid in ids
        if isinstance(hid, str) and hypothesis_title_by_id.get(hid)
    ]
    if not titles:
        return []
    return ["**Example Hypothesis Titles:**\n"] + [f"- {title}" for title in titles] + [""]


def _render_contact_group(
    group: dict[str, Any],
    contacts: list[dict[str, Any]],
    hypothesis_title_by_id: dict[str, str],
) -> list[str]:
    """Omit unmatched contact groups to avoid headings over empty bodies."""
    direction = _readable_text(group.get("research_direction"))
    if not direction or not contacts:
        return []
    lines = [f"### {direction}\n"]
    rationale = _readable_text(group.get("rationale"))
    if rationale:
        lines.append(f"**Why they are best for this direction:** {rationale}\n")
    lines += _render_group_example_titles(
        group.get("example_hypothesis_ids"), hypothesis_title_by_id
    )
    for contact in contacts:
        lines += _render_contact_entry(contact, heading="####", show_direction=False)
    return lines


def _partition_contacts_by_group(
    contacts: list[Any], group_keys: set[str]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    by_key: dict[str, list[dict[str, Any]]] = {key: [] for key in group_keys}
    ungrouped: list[dict[str, Any]] = []
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        key = _direction_key(contact.get("research_direction"))
        if key and key in by_key:
            by_key[key].append(contact)
        else:
            ungrouped.append(contact)
    return by_key, ungrouped


def _render_grouped_contacts(
    contacts: list[Any],
    groups: Any,
    hypothesis_title_by_id: dict[str, str],
) -> list[str]:
    """Ungrouped contacts retain their flat rendering, including older persisted
    reports.
    """
    valid_groups = [g for g in (groups or []) if isinstance(g, dict)]
    group_keys = {_direction_key(g.get("research_direction")) for g in valid_groups}
    by_key, ungrouped = _partition_contacts_by_group(contacts, group_keys)

    lines: list[str] = []
    for group in valid_groups:
        key = _direction_key(group.get("research_direction"))
        lines += _render_contact_group(group, by_key.get(key, []), hypothesis_title_by_id)
    for contact in ungrouped:
        lines += _render_contact_entry(contact)
    return lines


def _render_research_contacts_section(
    contacts: Any,
    groups: Any = None,
    hypothesis_title_by_id: dict[str, str] | None = None,
) -> list[str]:
    if not isinstance(contacts, list) or not contacts:
        return []
    lines = ["\n## Research Contacts\n"]
    lines += _render_grouped_contacts(contacts, groups, hypothesis_title_by_id or {})
    return lines


def _render_optional_paragraph(text: str | None) -> list[str]:
    return [f"{text}\n"] if text else []


def _render_experiments_list(experiments: list[Any]) -> list[str]:
    items = _readable_text_list(experiments)
    if not items:
        return []
    return ["Suggested experiments:\n"] + [f"- {experiment}" for experiment in items] + [""]


def _render_specific_questions(questions: list[Any]) -> list[str]:
    items = _readable_text_list(questions)
    if not items:
        return []
    return ["Specific questions:\n"] + [f"- {question}" for question in items] + [""]


def _render_sub_topic(sub_topic: dict[str, Any]) -> list[str]:
    if not isinstance(sub_topic, dict):
        return []
    title = _readable_text(sub_topic.get("title", ""))
    lines = [f"#### {title}\n"] if title else []
    why = _readable_text(sub_topic.get("why", ""))
    if why:
        lines.append(f"**Why:** {why}\n")
    what = _readable_text(sub_topic.get("what", ""))
    if what:
        lines.append(f"**What:** {what}\n")
    example_idea = _readable_text(sub_topic.get("example_idea", ""))
    if example_idea:
        lines.append(f"**Example idea:** {example_idea}\n")
    lines += _render_specific_questions(sub_topic.get("specific_questions") or [])
    return lines


def _render_sub_topics_list(sub_topics: list[Any]) -> list[str]:
    lines: list[str] = []
    for sub_topic in sub_topics:
        lines += _render_sub_topic(sub_topic)
    return lines


def _render_research_direction(direction: dict[str, Any]) -> list[str]:
    if not isinstance(direction, dict):
        return []
    lines = [f"### {_readable_text(direction.get('title', ''))}\n"]
    lines += _render_optional_paragraph(_readable_text(direction.get("importance", "")))
    recent_findings = _readable_text(direction.get("recent_findings", ""))
    if recent_findings:
        lines.append(f"**Recent findings:** {recent_findings}\n")
    lines += _render_experiments_list(direction.get("suggested_experiments") or [])
    lines += _render_sub_topics_list(direction.get("sub_topics") or [])
    return lines


def _has_overview_content(summary: str | None, directions: list[Any]) -> bool:
    return bool(summary or directions)


def _render_directions_list(directions: list[Any]) -> list[str]:
    lines: list[str] = []
    for direction in directions:
        lines += _render_research_direction(direction)
    return lines


def _direction_titles(directions: list[Any]) -> list[str]:
    titles = []
    for direction in directions:
        if not isinstance(direction, dict):
            continue
        title = _readable_text(direction.get("title", ""))
        if title:
            titles.append(title)
    return titles


def _render_directions_preview(directions: list[Any]) -> list[str]:
    titles = _direction_titles(directions)
    if len(titles) < 2:
        return []
    return (
        ["We will be focusing on these research directions:\n"]
        + [f"- {title}" for title in titles]
        + [""]
    )


def _render_unexpected_direction(direction: Any) -> str:
    if not isinstance(direction, dict):
        return ""
    title = _readable_text(direction.get("title", ""))
    if not title:
        return ""
    description = _readable_text(direction.get("description", ""))
    return f"- **{title}:** {description}" if description else f"- {title}"


def _render_unexpected_directions_section(directions: list[Any]) -> list[str]:
    lines = [line for direction in directions if (line := _render_unexpected_direction(direction))]
    if not lines:
        return []
    return ["\n### Unexpected research directions\n", *lines]


def _render_overview_section(ov: dict[str, Any], unexpected_directions: list[Any]) -> list[str]:
    if not isinstance(ov, dict):
        return []
    summary = _readable_text(ov.get("summary"))
    directions = ov.get("research_directions") or []
    unexpected_lines = _render_unexpected_directions_section(unexpected_directions)
    if not _has_overview_content(summary, directions) and not unexpected_lines:
        return []
    lines = ["\n## Research Overview\n"]
    lines += _render_optional_paragraph(summary)
    lines += _render_directions_preview(directions)
    lines += _render_directions_list(directions)
    lines += unexpected_lines
    return lines


def _render_pattern_list(heading: str, items: Any) -> list[str]:
    values = _readable_text_list(items)
    if not values:
        return []
    return [f"\n{heading}\n"] + [f"- {v}" for v in values]


def _render_open_questions_section(payload: dict[str, Any]) -> list[str]:
    questions = _readable_text_list(payload.get("open_questions") or [])
    clear_lines = _render_pattern_list("### Clear patterns", payload.get("clear_patterns") or [])
    unexpected_lines = _render_pattern_list(
        "### Unexpected patterns", payload.get("unexpected_patterns") or []
    )
    if not (questions or clear_lines or unexpected_lines):
        return []
    lines = ["\n## Open questions\n"]
    if questions:
        lines += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
        lines.append("")
    lines += clear_lines
    lines += unexpected_lines
    return lines


# Keep old persisted aims spellings readable until production reset.
_AIMS_PREAMBLE_BLOCKS = (
    ("disease_description", "Disease Description"),
    ("unmet_need", "Unmet Need"),
    ("proposed_solution", "Proposed Solution"),
)
_AIMS_CLOSING_BLOCKS = (("pilot_evaluation", "Pilot Evaluation"),)
_AIM_BODY_FIELDS = (
    ("overarching_goal", "Overarching goal"),
    ("hypothesis", "Hypothesis"),
    ("reasoning", "Reasoning"),
)


def _render_labeled_blocks(
    section: dict[str, Any], blocks: tuple[tuple[str, str], ...]
) -> list[str]:
    lines: list[str] = []
    for key, heading in blocks:
        text = _readable_text(section.get(key))
        if text:
            lines += [f"### {heading}\n", f"{text}\n"]
    return lines


def _render_nih_aim(aim: dict[str, Any], number: int) -> list[str]:
    if not isinstance(aim, dict):
        return []
    lines = [f"### Specific Aims {number}\n"]
    for key, label in _AIM_BODY_FIELDS:
        text = _readable_text(aim.get(key, ""))
        if text:
            lines.append(f"**{label}:** {text}\n")
    return lines


def _has_aims_content(preamble: list[str], aims: list[Any], closing: list[str]) -> bool:
    return bool(preamble or aims or closing)


def _render_aims_list(aims: list[Any]) -> list[str]:
    lines: list[str] = []
    for number, aim in enumerate(aims, 1):
        lines += _render_nih_aim(aim, number)
    return lines


def _render_nih_aims_section(aims_section: dict[str, Any]) -> list[str]:
    if not isinstance(aims_section, dict):
        return []
    preamble = _render_labeled_blocks(aims_section, _AIMS_PREAMBLE_BLOCKS)
    aims = aims_section.get("aims") or []
    closing = _render_labeled_blocks(aims_section, _AIMS_CLOSING_BLOCKS)
    if not _has_aims_content(preamble, aims, closing):
        return []
    return [
        "\n## NIH Specific Aims\n",
        *preamble,
        *_render_aims_list(aims),
        *closing,
    ]


def research_overview_sections(
    overview: dict[str, Any],
    hypothesis_title_by_id: dict[str, str] | None = None,
) -> list[list[str]]:
    # Structured model output can still carry malformed nested shapes; omit them
    # rather than fail report rendering.
    if not isinstance(overview, dict):
        return [[], [], [], []]
    return [
        _render_overview_section(
            overview.get("overview") or {},
            overview.get("unexpected_research_directions") or [],
        ),
        _render_open_questions_section(overview),
        _render_nih_aims_section(overview.get("nih_specific_aims") or {}),
        _render_research_contacts_section(
            overview.get("research_contacts") or [],
            overview.get("research_contact_groups"),
            hypothesis_title_by_id,
        ),
    ]


def _render_points(points: Any) -> list[str]:
    if not isinstance(points, list):
        return []
    return [f"  - {point}" for point in points if str(point).strip()]


def _sub_theme_headline(name: str, description: str) -> str:
    if name and description:
        return f"- **{name}**: {description}"
    return f"- {name or description}"


def _render_sub_theme(sub_theme: Any) -> list[str]:
    if not isinstance(sub_theme, dict):
        text = str(sub_theme).strip()
        return [f"- {text}"] if text else []
    name = str(sub_theme.get("theme") or "").strip()
    description = str(sub_theme.get("description") or "").strip()
    if not name and not description:
        return []
    return [
        _sub_theme_headline(name, description),
        *_render_points(sub_theme.get("points")),
    ]


def _render_sub_themes(sub_themes: Any) -> list[str]:
    if not isinstance(sub_themes, list):
        return []
    lines: list[str] = []
    for sub_theme in sub_themes:
        lines.extend(_render_sub_theme(sub_theme))
    return lines


def _render_theme(theme: dict[str, Any]) -> list[str]:
    """Separate prose from sub-theme bullets with a blank line so markdown
    reliably starts a list.
    """
    name = str(theme.get("theme") or "").strip()
    if not name:
        return []
    lines = [f"\n#### {name}\n"]
    description = str(theme.get("description") or "").strip()
    if description:
        lines.append(description)
    frequency = str(theme.get("frequency") or "").strip()
    if frequency:
        lines.append(f"*Frequency: {frequency}*")
    sub_themes = _render_sub_themes(theme.get("sub_themes"))
    if sub_themes:
        # Separate bullets from prose only when prose exists; otherwise the
        # heading already supplies its newline.
        lines.extend(["", *sub_themes] if len(lines) > 1 else sub_themes)
    return lines


def _render_theme_entry(theme: Any) -> list[str]:
    if isinstance(theme, dict):
        return _render_theme(theme)
    text = str(theme).strip()
    return [f"\n#### {text}\n"] if text else []


def render_emerging_themes(meta_review: dict[str, Any]) -> list[str]:
    """Legacy bare theme names carry no hierarchy; render their fallback as a
    flat list until production reset.
    """
    themes = meta_review.get("recurring_themes")
    if not themes:
        return _render_bare_theme_names(meta_review.get("emerging_themes"))
    lines: list[str] = []
    for theme in themes:
        lines.extend(_render_theme_entry(theme))
    if not lines:
        return []
    return ["\n### Emerging themes\n", *lines]


def _render_bare_theme_names(emerging_themes: Any) -> list[str]:
    if not isinstance(emerging_themes, list) or not emerging_themes:
        return []
    return ["\n### Emerging themes\n"] + [f"- {name}" for name in emerging_themes]


class _RecommendationFields(NamedTuple):
    """Legacy bare recommendations and structured recommendations must both
    remain readable until production reset.
    """

    body: str
    justification: str
    time_estimate: str
    phase_label: str
    recommended_idea: str


def _normalize_recommendation(
    rec: dict[str, Any] | str,
) -> _RecommendationFields:
    if not isinstance(rec, dict):
        return _RecommendationFields(str(rec), "", "", "", "")
    area = rec.get("focus_area", "")
    recommendation = rec.get("recommendation", "")
    body = f"**{area}**: {recommendation}" if area else str(recommendation)
    return _RecommendationFields(
        body=body,
        justification=str(rec.get("justification") or ""),
        # Roadmap steps refer to ideas by index, never by echoing model text;
        # legacy missing fields omit those details.
        time_estimate=str(rec.get("time_estimate") or "").strip(),
        phase_label=str(rec.get("phase_label") or "").strip(),
        recommended_idea=str(rec.get("recommended_idea") or "").strip(),
    )


def _render_recommendation(rec: dict[str, Any] | str, *, index: int | None = None) -> list[str]:
    fields = _normalize_recommendation(rec)
    prefix = f"{fields.phase_label}: " if fields.phase_label else ""
    suffix = f" ({fields.time_estimate})" if fields.time_estimate else ""
    label = "**Primary recommendation:**" if index is None else f"{index}."
    lines = [f"{label} {prefix}{fields.body}{suffix}"]
    if fields.justification:
        lines.append(f"  *{fields.justification}*")
    if fields.recommended_idea:
        lines.append(f"  Recommended idea: {fields.recommended_idea}")
    return lines


def _render_bullet_list(heading: str, items: list[Any]) -> list[str]:
    if not items:
        return []
    return [f"\n{heading}\n"] + [f"- {item}" for item in items]


_META_REVIEW_BULLET_SECTIONS = (
    ("common_strengths", "### Common strengths"),
    ("common_weaknesses", "### Common weaknesses"),
)


# Comparison axes follow the research domain; existing-solution comparisons can
# be empty when no landscape applies.


def _render_axis_values(axes: list[Any], values: Any) -> list[str]:
    """Pair values positionally with their own axes; unequal lengths must not
    produce unlabeled values.
    """
    if not isinstance(values, list):
        return []
    lines = []
    for axis, value in zip(axes, values, strict=False):
        axis_label = str(axis).strip()
        value_text = str(value).strip()
        if axis_label and value_text:
            lines.append(f"  - **{axis_label}:** {value_text}")
    return lines


def _render_comparison_row(
    row: Any,
    axes: list[Any],
    label_key: str,
) -> list[str]:
    if not isinstance(row, dict):
        return []
    label = str(row.get(label_key) or "").strip()
    if not label:
        return []
    lines = [f"- **{label}**"]
    lines += _render_axis_values(axes, row.get("values"))
    return lines


def _render_comparison(comparison: Any, *, existing_solutions: bool = False) -> list[str]:
    if not isinstance(comparison, dict):
        return []
    summary_key = "summary" if existing_solutions else "thematic_summary"
    rows_key = "rows" if existing_solutions else "ideas"
    label_key = "method" if existing_solutions else "idea"
    heading = (
        "Comparison to existing solutions"
        if existing_solutions
        else "Comparison of candidate ideas"
    )
    summary = str(comparison.get(summary_key) or "").strip()
    axes = comparison.get("axes") or []
    row_lines: list[str] = []
    for row in comparison.get(rows_key) or []:
        row_lines += _render_comparison_row(row, axes, label_key)
    if not (summary or row_lines):
        return []
    lines = [f"\n### {heading}\n"]
    if summary:
        lines += [summary, ""]
    lines += row_lines
    return lines


def _render_strategic_recommendations(recs: list[Any]) -> list[str]:
    """The schema has no named phases or dependencies; rendering them would
    invent roadmap content.
    """
    if not recs:
        return []
    lines = ["\n### Recommendation and strategic roadmap\n"]
    lines += _render_recommendation(recs[0])
    for i, rec in enumerate(recs[1:], start=1):
        lines += _render_recommendation(rec, index=i)
    return lines


def _render_related_hypotheses(related: list[str]) -> list[str]:
    if not related:
        return ["- **Related hypotheses:** (unspecified)"]
    return ["- **Related hypotheses:**"] + [f"  - {h}" for h in related]


def _render_connection(connection: Any) -> list[str]:
    if not isinstance(connection, dict):
        return []
    related = [str(h) for h in connection.get("related_hypotheses") or []]
    kind = str(connection.get("connection_type") or "")
    opportunity = str(connection.get("synthesis_opportunity") or "")
    if not (related or kind or opportunity):
        return []
    lines = _render_related_hypotheses(related)
    if kind:
        lines.append(f"  - **Connection type:** {kind}")
    if opportunity:
        lines.append(f"  - **Synthesis opportunity:** {opportunity}")
    return lines


def _render_unexpected_connections(connections: list[Any]) -> list[str]:
    lines: list[str] = []
    for connection in connections:
        lines += _render_connection(connection)
    if not lines:
        return []
    return ["\n### Unexpected connections\n", *lines]


def _render_main_research_directions_markdown(
    meta_review: dict[str, Any],
) -> list[str]:
    """Preserve model-written paragraph breaks; omit the heading when legacy or
    degraded output lacks this field.
    """
    text = str(meta_review.get("main_research_directions") or "").strip()
    if not text:
        return []
    return ["## Main Research Directions\n", text, ""]


def _render_meta_review_overview_markdown(
    meta_review: dict[str, Any],
) -> list[str]:
    """Omit empty headings when the synthesis contains only tournament-facing
    fields.
    """
    if not meta_review:
        return []
    body: list[str] = []
    body += _render_optional_paragraph(meta_review.get("summary"))
    for section_key, heading in _META_REVIEW_BULLET_SECTIONS:
        body += _render_bullet_list(heading, meta_review.get(section_key) or [])
    body += render_emerging_themes(meta_review)
    body += _render_unexpected_connections(meta_review.get("potential_connections") or [])
    if not body:
        return []
    return ["\n## Meta-review insights\n", *body]


def _render_meta_review_ranking_markdown(
    meta_review: dict[str, Any],
) -> list[str]:
    """Use a distinct heading for tournament-facing synthesis so navigation
    cannot imply a repeated section.
    """
    if not meta_review:
        return []
    body: list[str] = []
    body += _render_comparison(meta_review.get("candidate_comparison"))
    body += _render_comparison(
        meta_review.get("existing_solutions_comparison"),
        existing_solutions=True,
    )
    body += _render_strategic_recommendations(meta_review.get("strategic_recommendations") or [])
    if not body:
        return []
    return ["\n## Comparison and Recommendation\n", *body]
