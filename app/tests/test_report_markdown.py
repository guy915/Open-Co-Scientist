from typing import Any

import pytest
from co_scientist.domains.report.markdown.overview import research_overview_sections

from tests._report_helpers import render_markdown


def _evidence(evidence_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": evidence_id,
        "title": "RSK1 drives NHE1 phosphorylation",
        "url": "https://example.org/rsk1",
        "authors": ["Kim"],
        "year": 2022,
        "doi": None,
        "pmid": None,
        "retracted": False,
    }
    row.update(overrides)
    return row


def _overview_markdown(evidence: list[dict[str, Any]] | None) -> str:
    return render_markdown(evidence=evidence)


def _references(evidence: list[dict[str, Any]]) -> str:
    return _overview_markdown(evidence).split("## References", 1)[1]


@pytest.mark.parametrize(
    ("overrides", "present", "absent"),
    [
        (
            {},
            [
                "Kim et al., 2022",
                "RSK1 drives NHE1",
                "](https://example.org/rsk1)",
            ],
            [],
        ),
        (
            {"url": "", "authors": [], "year": None},
            ["RSK1 drives NHE1"],
            ["]("],
        ),
        ({"doi": "10.1038/example"}, ["DOI: 10.1038/example"], []),
        ({"pmid": "12345678"}, ["PMID: 12345678"], []),
        ({"retracted": True}, ["(retracted)"], []),
        ({"source_type": "preprint"}, ["(preprint)"], []),
        ({"source_type": "peer_reviewed"}, [], ["(peer", "(preprint)"]),
        (
            {
                "source_type": None,
                "source": "biorxiv",
                "url": "https://www.biorxiv.org/content/10.1101/1v1",
            },
            ["(preprint)"],
            [],
        ),
        ({"year": None, "source_type": "peer_reviewed"}, ["(no date)"], []),
        (
            {"year": None, "source_type": "document"},
            ["(attached document)"],
            ["(no date)"],
        ),
        (
            {"year": 9999, "source_type": "peer_reviewed"},
            ["(date not verifiable)"],
            [],
        ),
    ],
)
def test_a_reference_entry_says_what_the_source_is(
    overrides: dict[str, Any], present: list[str], absent: list[str]
) -> None:
    section = _references([_evidence("ev-1", **overrides)])

    for fragment in present:
        assert fragment in section
    for fragment in absent:
        assert fragment not in section


def test_repeated_captures_collapse_and_distinct_papers_survive() -> None:
    section = _references(
        [
            _evidence("ev-1", title="Paper D", doi="10.1000/shared"),
            _evidence("ev-2", title="Paper D (preprint)", doi="10.1000/shared"),
            _evidence("ev-3", title="Paper A", pmid="111"),
            _evidence("ev-4", title="Paper A (variant)", pmid="111"),
            _evidence("ev-5", title="Paper B", url="https://x.example/b"),
            _evidence("ev-6", title="Paper B (variant)", url="https://x.example/b"),
            _evidence("ev-7", title="Paper C", url="", authors=[], year=None),
            _evidence("ev-8", title="  paper c  ", url="", authors=[], year=None),
            _evidence("ev-9", title="Paper E", doi="10.1/e"),
            _evidence("ev-10", title="Paper F", doi="10.1/f"),
        ]
    )

    for title in ("Paper A", "Paper B", "Paper D"):
        assert section.count(title) == 1
    assert section.lower().count("paper c") == 1
    assert "Paper E" in section and "Paper F" in section


def _referenced_hypothesis(hyp_id: str, mechanism: str) -> dict[str, object]:
    return {
        "id": hyp_id,
        "title": "NHE1 coupling",
        "statement": "NHE1 couples to the RSK axis in HFpEF.",
        "mechanism": mechanism,
    }


def _citation_row(
    hyp_id: str, key: str, evidence_id: str, state: str = "verified"
) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "hypothesis_id": hyp_id,
        "evidence_id": evidence_id,
        "claim": f"[{key}] cited in hypothesis",
        "state": state,
    }


def _paper_evidence(evidence_id: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": evidence_id,
        "title": "RSK1 drives NHE1 phosphorylation",
        "url": "https://example.org/rsk1",
        "authors": ["Kim"],
        "year": 2022,
    }
    row.update(overrides)
    return row


def _bibliography_markdown(
    hypotheses: list[dict[str, object]],
    citations: list[dict[str, object]] | None = None,
    evidence: list[dict[str, object]] | None = None,
) -> str:
    return render_markdown(top_hypotheses=hypotheses, citations=citations, evidence=evidence)


def test_a_resolvable_key_prints_its_reference_entry_in_numeric_order() -> None:
    markdown = _bibliography_markdown(
        [
            _referenced_hypothesis("h1", "RSK1 acts on NHE1 [C1], confirmed in vivo [C2]."),
            _referenced_hypothesis("h2", "A second, uncited idea."),
        ],
        citations=[
            _citation_row("h1", "C2", "ev-2"),
            _citation_row("h1", "C1", "ev-1"),
        ],
        evidence=[
            _paper_evidence("ev-1", title="Paper one"),
            _paper_evidence("ev-2", title="Paper two"),
        ],
    )

    assert markdown.count("#### References") == 1
    section = markdown.split("#### References", 1)[1]
    assert section.index("[C1]") < section.index("[C2]")
    assert "Kim et al., 2022" in section
    assert "Paper one" in section and "Paper two" in section


@pytest.mark.parametrize(
    "citation",
    [
        {
            "run_id": "run-1",
            "hypothesis_id": "h1",
            "evidence_id": "ev-1",
            "claim": "NHE1 couples to the RSK axis.",
            "state": "verified",
        },
        _citation_row("h1", "C1", "ev-missing"),
    ],
    ids=["unparseable-claim", "dangling-evidence-id"],
)
def test_a_citation_that_cannot_be_resolved_fabricates_no_reference(
    citation: dict[str, object],
) -> None:
    markdown = _bibliography_markdown(
        [_referenced_hypothesis("h1", "RSK1 acts on NHE1 [C1].")],
        citations=[citation],
        evidence=[_paper_evidence("ev-1")],
    )

    assert "#### References" not in markdown


def _render_overview_payload(payload: dict[str, Any]) -> str:
    return "\n".join(line for section in research_overview_sections(payload) for line in section)


def test_a_well_formed_overview_renders_every_block() -> None:
    payload = {
        "overview": {
            "summary": "Top hypotheses converge on cross-pathway interference.",
            "research_directions": [
                {
                    "title": "Mitochondrial dysfunction",
                    "importance": "Central to the disease's early stages.",
                    "recent_findings": "mtDNA repair defects are implicated.",
                    "suggested_experiments": ["Profile ROS in patient iPSCs."],
                    "sub_topics": [
                        {
                            "title": "Mitochondrial DNA repair defects",
                            "why": "A deficiency could be a primary driver.",
                            "what": "Assay BER activity in iPSC neurons.",
                            "example_idea": "Knock down OGG1 in iPSC neurons.",
                            "specific_questions": [
                                "Does OGG1 activity correlate with damage?",
                            ],
                        }
                    ],
                },
                {
                    "title": "RNA processing defects",
                    "importance": "Implicated across ALS subtypes.",
                },
            ],
        },
        "nih_specific_aims": {
            "disease_description": "An aggressive malignancy.",
            "unmet_need": "Current therapies relapse.",
            "proposed_solution": "Repurpose an approved inhibitor.",
            "aims": [
                {
                    "overarching_goal": "Determine anti-tumour activity.",
                    "hypothesis": "Treatment reduces viability.",
                    "reasoning": "The pathway is upregulated.",
                }
            ],
            "pilot_evaluation": "A xenograft study measures tumour growth.",
        },
    }

    text = _render_overview_payload(payload)

    for fragment in (
        "converge on cross-pathway interference",
        "mtDNA repair defects are implicated.",
        "- Profile ROS in patient iPSCs.",
        "#### Mitochondrial DNA repair defects",
        "**Example idea:** Knock down OGG1 in iPSC neurons.",
        "- Does OGG1 activity correlate with damage?",
        "### Disease Description",
        "### Unmet Need",
        "### Proposed Solution",
        "### Specific Aims 1",
        "**Overarching goal:** Determine anti-tumour activity.",
        "**Hypothesis:** Treatment reduces viability.",
        "**Reasoning:** The pathway is upregulated.",
        "### Pilot Evaluation",
    ):
        assert fragment in text
    assert text.index("We will be focusing") < text.index("### Mitochondrial dysfunction")
    assert "- RNA processing defects" in text


def test_table_of_contents_lists_only_the_sections_this_render_produced() -> None:
    markdown = render_markdown(
        attributes=[{"name": "Human Relevance", "rubric": "1-5 scale."}],
        meta_review={"summary": "Ideas converge on a shared mechanism."},
        knowledge_base=[{"title": "NHE1", "summary": "Background."}],
    )

    lines = markdown.splitlines()
    toc_at = lines.index("#### Table of contents:")
    assert lines[toc_at + 1] == ""
    assert lines[toc_at + 2] == "- Stratification Attributes"
    assert lines[toc_at + 3] == "- Meta-review insights"
    assert lines[toc_at + 4] == "- Top hypotheses"
    assert lines[toc_at + 5] == "- Knowledge Base"
    assert "- Research Goal Details" not in markdown
    assert "- Open questions" not in markdown
    assert "- Research Contacts" not in markdown
