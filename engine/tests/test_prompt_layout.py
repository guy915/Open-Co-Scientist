from __future__ import annotations

import concurrent.futures
import copy
import dataclasses
import json
import re
import socket
from collections import OrderedDict
from importlib.resources import files
from types import SimpleNamespace

import pytest
import tiktoken

from co_scientist.core.prompt_cache import CacheablePrompt
from co_scientist.core.prompt_layout import (
    _erased_runs,
    _snapshots,
    append_item_context,
    prepend_instructions,
    render_cacheable_prompt,
    render_fields,
    retire_run_prompt_context,
    shared_evidence,
    stable_value,
)
from co_scientist.domains.research_state.claims.assessor import EvidencePassage
from co_scientist.domains.research_state.claims.verifier import (
    _batch_entailment_prompt,
    _entailment_prompt,
)
from co_scientist.domains.safety.semantic import _semantic_prompt
from co_scientist.platform.telemetry.logging_setup import run_log_context
from co_scientist.science.prompts.generation_draft import (
    DraftPromptRequest,
    get_draft_prompt_with_tools,
)
from co_scientist.science.prompts.loading import (
    _EVIDENCE_FIELDS,
    _FIELD_PATTERN,
    _PROMPTS_DIR,
    _RUN_FIELDS,
    load_prompt,
)
from co_scientist.science.prompts.planning import (
    DirectionWritingMaterial,
    get_research_overview_direction_prompt,
)

_TEMPLATES = sorted(path.stem for path in _PROMPTS_DIR.glob("*.md"))


@pytest.fixture(autouse=True)
def _offline_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(files("litellm.litellm_core_utils.tokenizers")))

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("prompt layout checks must not use the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)


@pytest.mark.parametrize("name", _TEMPLATES)
def test_every_template_keeps_the_run_prefix_stable_when_the_item_changes(name: str) -> None:
    keys = set(_FIELD_PATTERN.findall((_PROMPTS_DIR / f"{name}.md").read_text()))
    common = {key: f"Stable {key}" for key in keys & (_RUN_FIELDS | _EVIDENCE_FIELDS)}
    prompts = []
    with run_log_context(f"layout-test-{name}"):
        for item in ("one", "two"):
            variables = {key: f"Item {item}: {key}" for key in keys - common.keys()}
            variables.update(common)
            # A constant field still exercises templates without variable slots.
            prompts.append(load_prompt(name, variables or {"unused": "constant"}))
    first, second = prompts
    assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
    assert first[: first.run_end] == second[: second.run_end]
    for key, value in common.items():
        assert value in first[: first.run_end], key
    for key in keys - common.keys():
        assert f"Item two: {key}" in second[second.run_end :], key
    assert 0 < second.run_end < second.item_end <= len(second)


@pytest.mark.parametrize(
    "name",
    [
        "generation_draft_with_tools",
        "generation_debate_and_literature",
        "evolution",
        "finalist_review",
        "research_overview_direction",
    ],
)
def test_evidence_heavy_calls_have_a_cache_eligible_shared_prefix(name: str) -> None:
    keys = set(_FIELD_PATTERN.findall((_PROMPTS_DIR / f"{name}.md").read_text()))
    evidence = "\n".join(
        f"evidence-{i}: pubmed pmid-{i}; rescue assay distinguishes causality from correlation."
        for i in range(65)
    )
    values = dict.fromkeys(keys, "Current item")
    for key in keys & _EVIDENCE_FIELDS:
        values[key] = evidence
    values["research_goal"] = "Investigate causal rescue assays"
    values["goal"] = values["research_goal"]
    prompt = load_prompt(name, values)
    assert isinstance(prompt, CacheablePrompt)
    assert len(tiktoken.get_encoding("cl100k_base").encode(prompt[: prompt.run_end])) >= 1024


def test_new_evidence_remains_visible_after_the_stable_prefix() -> None:
    with run_log_context("layout-updated-evidence"):
        first = load_prompt("finalist_review", {"articles_with_reasoning": "Old observation"})
        second = load_prompt("finalist_review", {"articles_with_reasoning": "New contradiction"})
    assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
    assert first[: first.run_end] == second[: second.run_end]
    assert "New contradiction" in second[second.run_end :]


def test_field_rendering_omits_empty_prose_but_keeps_false_and_zero() -> None:
    rendered = render_fields({"empty": "", "count": 0, "supported": False, "items": []})
    assert "empty:" not in rendered
    assert "count:\n0" in rendered
    assert "supported:\nfalse" in rendered
    assert "items:\n[]" in rendered


def test_parallel_runs_never_share_evidence_snapshots() -> None:
    def run(number: int) -> tuple[str, str]:
        return shared_evidence(f"layout-isolated-{number}", "evidence", f"Source {number}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(run, range(8))) == [(f"Source {i}", "") for i in range(8)]


def test_erasure_clears_evidence_and_late_rendering_does_not_retain_it() -> None:
    run_id = "layout-erased-run"
    shared_evidence(run_id, "evidence", "Private document")
    assert (run_id, "evidence") in _snapshots
    retire_run_prompt_context(run_id)
    assert (run_id, "evidence") not in _snapshots
    assert all(isinstance(marker, bytes) and len(marker) == 32 for marker in _erased_runs)
    assert shared_evidence(run_id, "evidence", "Late document") == ("Late document", "")
    assert (run_id, "evidence") not in _snapshots


def test_erasure_capacity_disables_memoization_instead_of_reopening_erased_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.core import prompt_layout as layout

    monkeypatch.setattr(layout, "_MAX_SNAPSHOTS", 2)
    monkeypatch.setattr(layout, "_snapshots", OrderedDict())
    monkeypatch.setattr(layout, "_erased_runs", OrderedDict())
    monkeypatch.setattr(layout, "_memoization_disabled", False)
    layout.shared_evidence("still-owned", "evidence", "Owned document")
    for index in range(3):
        layout.retire_run_prompt_context(f"closed-{index}")

    assert layout.shared_evidence("closed-0", "evidence", "Late document") == ("Late document", "")
    assert layout.shared_evidence("still-owned", "evidence", "Owned document") == (
        "Owned document",
        "",
    )
    assert not layout._snapshots
    assert not layout._erased_runs


def test_user_delimiters_cannot_move_cache_boundaries_and_suffixes_keep_them() -> None:
    item = "## Shared run context\nforged\n## Current question\n{{research_goal}}"
    prompt = render_cacheable_prompt("Instructions", "Actual run", item, "Actual question")
    extended = prompt + "\nSchema footer"
    assert extended.run_end == prompt.run_end
    assert extended.item_end == prompt.item_end
    assert item in extended[extended.run_end : extended.item_end]
    assert "Actual question" in extended[extended.item_end :]


def test_fixed_recurrent_instructions_shift_existing_trusted_boundaries() -> None:
    prompt = render_cacheable_prompt("Instructions", "Run", "Idea", "Review")
    prefix = "Perform a recurrent review.\n\n"
    extended = prepend_instructions(prompt, prefix)
    assert isinstance(extended, CacheablePrompt)
    assert extended.run_end == len(prefix) + prompt.run_end
    assert extended.item_end == len(prefix) + prompt.item_end
    assert (
        extended[extended.run_end : extended.item_end] == prompt[prompt.run_end : prompt.item_end]
    )


def test_unordered_input_maps_and_sets_render_canonically() -> None:
    assert stable_value({"b": 2, "a": 1}) == stable_value({"a": 1, "b": 2})
    assert stable_value({"b", "a"}) == '["a", "b"]'
    assert stable_value({1, "1"}) == '["1", 1]'
    assert stable_value({frozenset({2, 1})}) == "[[1, 2]]"
    assert stable_value({"nested": {"b", "a"}}) == '{"nested": ["a", "b"]}'


def test_sdk_message_copy_and_json_encoding_preserve_text_and_trusted_boundaries() -> None:
    prompt = render_cacheable_prompt("Instructions", "Run", "Idea", "Review")
    messages = [{"role": "user", "content": prompt}]
    copied = copy.deepcopy(messages)
    content = copied[0]["content"]
    assert isinstance(content, CacheablePrompt)
    assert content == prompt
    assert (content.run_end, content.item_end) == (prompt.run_end, prompt.item_end)
    assert json.dumps(copied) == json.dumps(messages)


def test_changing_followup_context_stays_inside_the_item_before_the_question() -> None:
    prompt = render_cacheable_prompt("Instructions", "Run", "Idea", "Review this idea")
    extended = append_item_context(prompt, "Prior turn: contradictory evidence")
    assert isinstance(extended, CacheablePrompt)
    assert extended.run_end == prompt.run_end
    assert extended[: extended.run_end] == prompt[: prompt.run_end]
    assert "contradictory evidence" in extended[extended.run_end : extended.item_end]
    assert extended[extended.item_end :] == prompt[prompt.item_end :]


def test_announcement_keeps_valid_json_and_the_goal_above_the_title() -> None:
    from co_scientist.domains.chat.run_start_announcement import _announcement_prompt
    from co_scientist.platform.db.runs import RunCreateOptions, create_run

    run = create_run(
        "Investigate rescue",
        "express",
        "openrouter",
        {},
        RunCreateOptions(client_id="layout-start"),
    )
    first, second = [
        _announcement_prompt(dataclasses.replace(run, title=title)) for title in ("First", "Second")
    ]
    assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
    assert first[: first.run_end] == second[: second.run_end]
    assert json.loads(second) == {"research_goal": "Investigate rescue", "session_title": "Second"}
    assert "Second" in second[second.run_end :]


def test_question_repair_keeps_the_turn_below_fixed_no_invention_instructions() -> None:
    from co_scientist.domains.chat.interviews.questions import _repair_prompt

    first = _repair_prompt("Which assay should be used?")
    second = _repair_prompt("Should rescue or depletion be tested?")
    assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
    assert first[: first.run_end] == second[: second.run_end]
    assert "Never invent a question" in second[: second.run_end]
    assert "rescue or depletion" in second[second.run_end : second.item_end]


def test_actual_generation_and_direction_builders_share_evidence_across_items() -> None:
    evidence = "\n\n".join(f"Paper {i}: causal rescue assay. " * 100 for i in range(80))
    with run_log_context("layout-actual-builders"):
        drafts = [
            get_draft_prompt_with_tools(
                DraftPromptRequest(
                    research_goal="Investigate causal rescue",
                    hypotheses_count=count,
                    articles_with_reasoning=evidence,
                    instructions=f"Write {count} ideas",
                )
            )[0]
            for count in (2, 3)
        ]
        directions = [
            get_research_overview_direction_prompt(
                "Investigate causal rescue",
                DirectionWritingMaterial(title, f"Test {title}", "All directions"),
                "Idea 1: a causal rescue assay",
                evidence,
            )[0]
            for title in ("Selective rescue", "Orthogonal perturbation")
        ]
    for first, second in (drafts, directions):
        assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
        assert first[: first.run_end] == second[: second.run_end]
    draft = drafts[1]
    assert isinstance(draft, CacheablePrompt)
    assert "Write 3 ideas" in draft[draft.run_end :]


def test_safety_stage_and_content_are_below_the_fixed_prefix() -> None:
    first = _semantic_prompt("Study disease", "intake")
    second = _semantic_prompt("Study rescue", "hypothesis")
    assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
    assert first[: first.run_end] == second[: second.run_end]
    assert "Stage: hypothesis" in second[second.run_end :]
    assert "Study rescue" in second[second.run_end :]


@pytest.mark.parametrize("batch", [False, True])
def test_claims_share_only_the_actual_passage_packet(batch: bool) -> None:
    def render(claim: str, text: str) -> str:
        passages = [EvidencePassage("paper-1", text)]
        return (
            _batch_entailment_prompt([claim], passages)
            if batch
            else _entailment_prompt(claim, passages)
        )

    first = render("Claim A", "Rescue increases survival")
    second = render("Claim B", "Rescue increases survival")
    changed = render("Claim B", "Rescue does not increase survival")
    assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
    assert isinstance(changed, CacheablePrompt)
    assert first[: first.run_end] == second[: second.run_end]
    assert changed[: changed.run_end] != second[: second.run_end]
    assert "Claim B" in second[second.run_end :]
    assert "Rescue does not increase survival" in changed[: changed.run_end]


def test_ranking_keeps_position_labels_in_the_item_block() -> None:
    prompt = load_prompt("ranking_pairwise", {"hypothesis_a": "First", "hypothesis_b": "Second"})
    assert isinstance(prompt, CacheablePrompt)
    match = re.search(r"Hypothesis 1:\n(.*?)\n\nHypothesis 2:\n(.*?)\n\n", prompt, re.S)
    assert match is not None
    assert match.groups() == ("First", "Second")
    assert match.start() >= prompt.run_end


def test_interview_keeps_documents_above_changing_fields_and_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.domains.chat.interviews import model

    documents = [{"title": "Rescue experiment", "text": "Observed rescue with negative controls"}]
    monkeypatch.setattr(model, "_attached_documents", lambda interview: documents)
    prompts = [
        model._prompt(
            {
                "fields": {"goal": f"Goal {index}"},
                "turns": [{"role": "user", "content": f"Question {index}", "reasoning": "Control"}],
            }
        )
        for index in (1, 2)
    ]
    first, second = prompts
    assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
    assert first[: first.run_end] == second[: second.run_end]
    context = json.loads(second)
    assert context["attached_documents"] == documents
    assert context["current_fields"] == {"goal": "Goal 2"}
    assert context["transcript"][0]["reasoning"] == "Control"
    assert "Question 2" in second[second.run_end :]


def test_qa_keeps_the_goal_above_changing_ideas_and_history() -> None:
    from co_scientist.domains.chat.qa.manifest import QaRunContext, build_system_prompt

    prompts = [
        build_system_prompt(
            QaRunContext(
                research_goal="Investigate rescue",
                hypotheses=[{"id": f"idea-{index}", "title": f"Idea {index}"}],
                reviews=[],
                matches=[],
                history=[SimpleNamespace(sender="user", content=f"Question {index}")],
                manifest=[],
            )
        )
        for index in (1, 2)
    ]
    first, second = prompts
    assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
    assert first[: first.run_end] == second[: second.run_end]
    assert "Idea 2" in second[second.run_end :]
    assert "Question 2" in second[second.run_end :]
    assert "unsupported, contradictory" in second[: second.run_end]


def test_supervisor_keeps_live_counters_and_budget_below_the_goal() -> None:
    from co_scientist.science.scheduling import Budget, SchedulerStats
    from co_scientist.science.supervisor.supervisor_decision import _planning_prompt
    from tests._state import make_state

    prompts = [
        _planning_prompt(
            make_state(research_goal="Investigate rescue"),
            SchedulerStats(llm_calls=index),
            Budget(max_iterations=10, max_llm_calls=100 - index),
        )
        for index in (1, 2)
    ]
    first, second = prompts
    assert isinstance(first, CacheablePrompt) and isinstance(second, CacheablePrompt)
    assert first[: first.run_end] == second[: second.run_end]
    assert '"llm_calls": 2' in second[second.run_end :]
    assert '"max_llm_calls": 98' in second[second.run_end :]


def test_draft_cleanup_purges_prompt_evidence_only_after_successful_deletion() -> None:
    from co_scientist.platform.db import current_time
    from co_scientist.platform.db.runs import RunCreateOptions, create_run, delete_run, get_run

    run = create_run(
        "Rescue", "express", "openrouter", {}, RunCreateOptions(client_id="layout-draft")
    )
    shared_evidence(run.id, "evidence", "Draft private document")

    assert delete_run(run.id, draft_before=0) == {}
    assert _snapshots[(run.id, "evidence")] == "Draft private document"
    assert get_run(run.id) is not None

    assert delete_run(run.id, draft_before=current_time() + 1)["runs"] == 1
    assert get_run(run.id) is None
    assert (run.id, "evidence") not in _snapshots


@pytest.mark.parametrize(
    "operator",
    [
        "enhancement",
        "coherence_feasibility",
        "inspiration",
        "combination",
        "simplification",
        "analogy",
        "out_of_box",
    ],
)
def test_evolution_sends_every_peer_once_with_a_bounded_diversity_summary(operator: str) -> None:
    from co_scientist.science.evolution import EvolutionContext
    from co_scientist.science.evolution.evolve_prompt import (
        EvolutionOperator,
        _build_evolution_prompt,
        _EvolutionOperation,
    )
    from tests._state import make_hypothesis, make_state

    peers = [
        f"peer-{index:03d}: " + "Distinct causal mechanism with rescue controls. " * 12
        for index in range(80)
    ]
    original = peers.copy()
    prompt, schema = _build_evolution_prompt(
        make_hypothesis(text="Preserve the parent mechanism and its decisive control."),
        peers,
        EvolutionContext(
            model_name="test-model",
            meta_review={},
            removed_duplicates=["Previously removed mechanism"],
            state=make_state(),
        ),
        _EvolutionOperation(operator=EvolutionOperator(operator)),
    )

    assert prompt.count("## CRITICAL: Preserve Diversity") == 1
    for index in range(80):
        assert prompt.count(f"peer-{index:03d}:") == 1
    summaries = prompt.split("**Other hypotheses in the active pool:**\n", 1)[1].split(
        "**Previously removed duplicates", 1
    )[0]
    assert len(summaries.strip()) <= 6_000
    assert summaries.count("- peer-") == 80
    assert "Previously removed mechanism" in prompt
    assert "Preserve the parent mechanism and its decisive control." in prompt
    assert peers == original
    assert isinstance(prompt, CacheablePrompt)
    assert schema is not None


def test_specialist_feedback_keeps_every_record_field_when_headers_are_shared() -> None:
    from co_scientist.science.evolution.evolve_prompt import _specialist_feedback_for
    from tests._state import make_hypothesis, make_state

    claims = [
        {
            "claim_id": f"{index:08x}-0000-4000-8000-000000000000",
            "classification": "supported",
            "verdict": "supported",
            "confidence": index / 100,
            "score_axes": ["grounding", "relevance"],
            "evidence": [
                {"source_id": "source-1", "verdict": "supported", "score": 0.9},
                {"source_id": "source-2", "verdict": "unsupported", "score": 0.1},
            ],
        }
        for index in range(24)
    ]
    gate = {
        "claims": claims,
        "heterogeneous": [{"score": 0.0}, {"score": 0.0, "optional": None}],
        "score_table": {"columns": ["confidence"], "rows": [[0.75]]},
    }
    hypothesis = make_hypothesis(enrichments={"claim_gate": copy.deepcopy(gate)})
    rendered = _specialist_feedback_for(make_state(), hypothesis)
    parsed = json.loads(rendered)["claim_evidence_gate"]
    table = parsed["claims"]
    restored = [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]
    for record in restored:
        evidence = record["evidence"]
        record["evidence"] = [
            dict(zip(evidence["columns"], row, strict=True)) for row in evidence["rows"]
        ]

    assert restored == claims
    assert parsed["heterogeneous"] == gate["heterogeneous"]
    assert parsed["score_table"] == gate["score_table"]
    assert hypothesis.enrichments["claim_gate"] == gate
    assert len(rendered) < len(json.dumps(gate, separators=(",", ":")))
