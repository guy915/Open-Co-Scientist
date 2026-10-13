from __future__ import annotations

from typing import Any

import pytest
from co_scientist.domains.report import build as report_build
from co_scientist.domains.report import content as report_content
from co_scientist.domains.report import gates as report_gates
from co_scientist.domains.research_state.claims.gate import ClaimEdge
from co_scientist.platform.db import retrieval_calls as retrieval
from co_scientist.platform.db import runs
from co_scientist.platform.db.retrieval_calls import NewRetrievalCall

from tests._report_helpers import render_markdown
from tests._store_helpers import seed_run


@pytest.mark.parametrize(
    ("ideas", "tally", "expected"),
    [
        (
            15,
            {"review_rejected": 15},
            "No hypothesis could be published: of 15 ideas, 15 were rejected "
            "by peer review before ranking (the reviewer judged them "
            "inaccurate, non-novel, unsafe, or evidence-blocked).",
        ),
        (
            5,
            {"safety": 5},
            "No hypothesis could be published: of 5 ideas, 5 were withheld by the safety review.",
        ),
        (
            3,
            {"contradicted": 3},
            "No hypothesis could be published: of 3 ideas, 3 were contradicted by the evidence.",
        ),
        (
            0,
            {},
            "No hypothesis could be published: the run produced no ideas.",
        ),
    ],
)
def test_empty_leaderboard_reason_names_only_what_withheld_the_ideas(
    ideas: int, tally: dict[str, int], expected: str
) -> None:
    full = {
        "review_rejected": 0,
        "duplicate": 0,
        "contradicted": 0,
        "safety": 0,
    }

    assert report_gates._empty_leaderboard_reason(ideas, full | tally) == expected


def _leaderboard_hypothesis(identifier: str, title: str) -> dict[str, object]:
    return {
        "id": identifier,
        "title": title,
        "statement": f"{title} changes the measured phenotype.",
        "safety_status": "allowed",
    }


def _contradicted(hypothesis_id: str, claim: str, role: str) -> ClaimEdge:
    row = {
        "hypothesis_id": hypothesis_id,
        "claim": claim,
        "label": "contradicts",
        "claim_role": role,
        "supporting": [],
        "contradicting": [{"evidence_id": "ev1", "quote": "A cited passage."}],
        "assessor": "llm",
    }
    return ClaimEdge.from_row(row)


def test_a_contradicted_proposal_is_not_reported_as_withheld() -> None:
    # Proposal contradictions publish as findings; only categorical
    # contradictions withhold ideas.
    withheld = _leaderboard_hypothesis("h2", "Unsupported bypass")
    proposing = _leaderboard_hypothesis("h3", "Speculative bypass")
    edges = [
        _contradicted("h2", "The bypass is constitutively active.", "categorical"),
        _contradicted("h3", "Blocking the loop may raise the flux.", "speculative"),
    ]

    published = report_gates.exclude_unsafe_hypotheses("run-1", [withheld, proposing], None, edges)
    insights = report_content._agent_insights(published, edges, {})

    assert [hyp["id"] for hyp in published] == ["h3"]
    by_claim = {entry.split(" (", 1)[0]: entry for entry in insights["contradictions"]}
    assert "withheld" in by_claim["The bypass is constitutively active."]
    assert "withheld" not in by_claim["Blocking the loop may raise the flux."]


def _sections_hypothesis(identifier: str, title: str) -> dict[str, object]:
    return {
        "id": identifier,
        "title": title,
        "statement": f"{title} changes the measured phenotype.",
        "mechanism": f"{title} acts through a causal feedback mechanism.",
        "experimental_context": f"Perturb {title} with matched controls.",
        "safety_status": "allowed",
    }


def _edge(
    hypothesis_id: str,
    claim: str,
    label: str,
    evidence_ids: tuple[str, ...] = (),
) -> ClaimEdge:
    # Fixtures must use persisted claim/supporting keys to expose drift in the
    # real store contract.
    row = {
        "hypothesis_id": hypothesis_id,
        "claim": claim,
        "label": label,
        "claim_role": "categorical",
        "supporting": [
            {"evidence_id": evidence_id, "quote": "A cited passage."}
            for evidence_id in evidence_ids
        ],
        "contradicting": [],
        "assessor": "llm",
    }
    return ClaimEdge.from_row(row)


def test_goal_report_sections_preserve_claim_grounding() -> None:
    released = _sections_hypothesis("h1", "Feedback control")
    rejected = _sections_hypothesis("h2", "Unsupported bypass")
    edges = [
        _edge("h1", "Feedback is rate-limiting.", "supports", ("ev1",)),
        _edge("h2", "The bypass is constitutively active.", "contradicts"),
    ]

    topics = report_content._knowledge_base_topics([released], edges)
    insights = report_content._agent_insights(
        [released],
        edges,
        {"common_weaknesses": ["Cell-type specificity remains uncertain."]},
    )
    buckets = report_content._idea_buckets([released], [released, rejected], edges)

    assert topics[0]["title"] == "Feedback control"
    assert topics[0]["reference_ids"] == ["ev1"]
    assert len(insights["contradictions"]) == 1
    assert insights["contradictions"][0].startswith("The bypass is constitutively active.")
    assert insights["uncertainties"] == ["Cell-type specificity remains uncertain."]
    assert buckets["high_potential"][0]["id"] == "h1"
    assert buckets["non_viable"][0]["id"] == "h2"
    assert "Evidence verification" in buckets["non_viable"][0]["reason"]


def test_synthesized_topics_map_only_to_persisted_evidence() -> None:
    overview = {
        "knowledge_base": [
            {
                "id": "topic-1",
                "title": "Cross-source mechanism",
                "summary": "Two findings converge.",
                "detail": "A detailed synthesis.",
                "uncertainty": "The causal direction remains uncertain.",
                "references": [
                    {"title": "Persisted study"},
                    {"title": "Missing study"},
                ],
            },
            {
                "id": "topic-2",
                "title": "Unsupported synthesis",
                "references": [{"title": "Missing study"}],
            },
        ]
    }
    topics = report_content._synthesized_knowledge_base_topics(
        overview, [{"id": "ev-1", "title": "Persisted study"}]
    )

    assert len(topics) == 1
    assert topics[0]["reference_ids"] == ["ev-1"]
    assert topics[0]["uncertainty"].startswith("The causal direction")


def test_the_overview_document_carries_the_about_disclosure() -> None:
    markdown = render_markdown(top_hypotheses=[])

    assert (
        "**About**: *This is an experimental system for generating novel"
        " and testable hypotheses. The hypotheses are generated by a"
        " model and may be wrong. For research purposes only.*"
    ) in markdown


def _call(source: str, question: str, question_id: str, query: str = "q") -> dict[str, Any]:
    return {
        "source": source,
        "question": question,
        "question_id": question_id,
        "query": query,
    }


async def test_a_built_report_pulls_its_own_runs_retrieval_calls(
    isolated_db: str,
) -> None:
    run = seed_run("cardiac goal")
    retrieval.add_retrieval_calls(
        [
            NewRetrievalCall(
                run_id=run.id,
                id="c1",
                question="What drives fibrosis?",
                question_id="q1",
                query="fibrosis mechanism",
                source="pubmed",
                depth=1,
                status="ok",
            )
        ],
        db_path=isolated_db,
    )

    built = await report_build.build_report_content(
        run.id,
        report_build.ReportRequest(
            research_goal=run.research_goal,
            run_mode="standard",
            provider="engine",
            db_path=isolated_db,
        ),
    )

    assert "## Data sources" in built.markdown
    assert "pubmed" in built.markdown
    assert "What drives fibrosis?" in built.markdown


def test_the_header_carries_goal_requirements_attributes_and_criteria() -> None:
    markdown = render_markdown(
        top_hypotheses=[],
        setup={
            "goal": "Explain the cardiac benefit.",
            "requirements": ["Must be testable in vitro."],
            "attributes": ["Mechanistically specific"],
            "criteria": ["Scientific soundness"],
        },
    )

    assert "## Research Goal Details" in markdown
    assert "**Goal:** Explain the cardiac benefit." in markdown
    assert "**Requirements:**" in markdown
    assert "Must be testable in vitro." in markdown
    assert "**Attributes:**" in markdown
    assert "Mechanistically specific" in markdown
    assert "**Criteria:**" in markdown
    assert "Scientific soundness" in markdown


def test_redact_run_goal_clears_the_restatement(isolated_db: str) -> None:
    # Redaction must clear the pre-screen restatement too, or it republishes the
    # goal in different words.
    run = seed_run("Synthesize a controlled pathogen.", client_id="c1", db_path=isolated_db)
    runs.set_run_goal_restatement(run.id, "A paraphrase of the goal.", db_path=isolated_db)

    runs.redact_run_goal(run.id, "[redacted]", "[redacted]", db_path=isolated_db)

    reloaded = runs.get_run(run.id, db_path=isolated_db)
    assert reloaded is not None
    assert reloaded.goal_restatement is None


# Themes must be real headings so outlines retain the taxonomy rather than
# flattening it.


def _themed(theme: str, title: str, detail: str) -> dict[str, object]:
    return {
        "id": f"topic-{title}",
        "theme": theme,
        "title": title,
        "summary": "",
        "detail": detail,
        "uncertainty": "",
        "reference_ids": ["ev-1"],
    }


def test_a_theme_is_printed_once_above_its_sections() -> None:
    markdown = render_markdown(
        knowledge_base=[
            _themed("Matrix Architecture", "Cross-Linking", "Dense prose."),
            _themed("Matrix Architecture", "Stiffness", "More prose."),
            _themed("Immune Niche", "Macrophages", "Other prose."),
        ]
    )

    section = markdown.split("## Knowledge Base")[1]
    assert section.count("### Matrix Architecture") == 1
    assert section.count("### Immune Niche") == 1
    assert "#### Cross-Linking" in section
    assert "#### Stiffness" in section
    assert "#### Macrophages" in section
    assert "**" not in section
