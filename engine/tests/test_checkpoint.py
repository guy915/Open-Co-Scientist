from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import co_scientist.orchestration.checkpoint as checkpoint_module
from co_scientist.domains.research_state.models import (
    ExecutionMetrics,
    Hypothesis,
    HypothesisOrigin,
    HypothesisReview,
)
from co_scientist.domains.research_state.state import (
    AppendHypotheses,
    accumulate_matchups,
    deduplicate_hypotheses,
)
from co_scientist.orchestration.checkpoint import (
    CheckpointSchemaError,
    restore_workflow_state,
    serialize_workflow_state,
)
from co_scientist.platform.db.checkpoints import CHECKPOINT_VERSION
from co_scientist.platform.sandbox.patch import (
    Patch,
    PatchError,
    apply_patch,
    parse_patch,
    seek_anchor,
)
from tests._state import make_hypothesis


def _rich_hypotheses() -> list[Hypothesis]:
    """Coverage is owed only to reviewed ideas; new entrants first owe a
    review."""
    parent = Hypothesis(text="parent hypothesis", elo_rating=1240)
    child = Hypothesis(
        text="child hypothesis",
        parent_id=parent.id,
        generation=1,
        origin=HypothesisOrigin.EVOLUTION,
        reviews=[
            HypothesisReview(
                review_summary="ok",
                scores={"novelty": 4},
                safety_ethical_concerns="none",
                detailed_feedback={"a": "b"},
                constructive_feedback="tighten",
                overall_score=4.0,
            )
        ],
        win_count=2,
        loss_count=1,
    )
    return [parent, child]


def _rich_state() -> dict[str, object]:
    return {
        "research_goal": "Explain X",
        "model_name": "fake/model",
        "supervisor_model_name": "fake/model",
        "max_iterations": 3,
        "hypotheses": _rich_hypotheses(),
        "current_iteration": 2,
        "task_history": [{"task_type": "evolve", "reason": "leaders"}],
        "next_task": "generate",
        "termination_reason": None,
        "budget": {"max_iterations": 3, "max_llm_calls": 100},
        "orchestrator_state": {"prev_top_elo": 1240, "rank_stable_cycles": 1},
        "meta_review": {"summary": "s"},
        "proximity_graph": {"edges": [], "meta": {"edge_count": 0}},
        "tournament_matchups": [{"winner": "a"}],
        "metrics": ExecutionMetrics(llm_calls=17, reviews_count=4),
        "start_time": 1000.0,
        "run_id": "run-123",
        "messages": [{"role": "assistant", "content": "hi"}],
        "progress_callback": lambda *a: None,
        "tool_registry": object(),
    }


def test_round_trip_preserves_serializable_state() -> None:
    state = _rich_state()
    checkpoint = serialize_workflow_state(state, last_event_seq=42)
    restored = restore_workflow_state(json.loads(json.dumps(checkpoint)))

    assert restored["research_goal"] == "Explain X"
    assert restored["current_iteration"] == 2
    assert restored["next_task"] == "generate"
    assert restored["budget"] == {"max_iterations": 3, "max_llm_calls": 100}
    assert restored["task_history"] == state["task_history"]
    assert restored["orchestrator_state"] == state["orchestrator_state"]

    hyps = restored["hypotheses"]
    assert [h.text for h in hyps] == ["parent hypothesis", "child hypothesis"]
    child = hyps[1]
    assert child.parent_id == hyps[0].id
    assert child.generation == 1
    assert child.origin is HypothesisOrigin.EVOLUTION
    assert child.win_count == 2 and child.loss_count == 1
    assert child.reviews[0].overall_score == 4.0

    assert restored["metrics"].llm_calls == 17
    assert restored["metrics"].reviews_count == 4
    assert restored["resume"] is True
    assert checkpoint["last_event_seq"] == 42
    assert restored["run_id"] == "run-123"


def test_runtime_handles_excluded_and_reinjected() -> None:
    state = _rich_state()
    checkpoint = serialize_workflow_state(state, last_event_seq=1)

    assert "progress_callback" not in checkpoint["state"]
    assert "tool_registry" not in checkpoint["state"]

    sentinel_cb = lambda *a: "cb"  # noqa: E731
    sentinel_registry = object()
    restored = restore_workflow_state(
        checkpoint,
        progress_callback=sentinel_cb,
        tool_registry=sentinel_registry,
    )
    assert restored["progress_callback"] is sentinel_cb
    assert restored["tool_registry"] is sentinel_registry


def _freeze_time(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    monkeypatch.setattr(checkpoint_module, "time", types.SimpleNamespace(time=lambda: now))


def test_restore_rebases_start_time_excluding_idle_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paused wall-clock time must not exhaust the resumed run's active-time
    budget."""
    state = _rich_state()
    state["start_time"] = 1000.0
    _freeze_time(monkeypatch, 1300.0)
    checkpoint = serialize_workflow_state(state, last_event_seq=1)

    assert "start_time" not in checkpoint["state"]
    assert checkpoint["state"]["elapsed_active_s"] == pytest.approx(300.0)

    _freeze_time(monkeypatch, 500_000.0)
    restored = restore_workflow_state(checkpoint)
    assert 500_000.0 - restored["start_time"] == pytest.approx(300.0)


def test_incompatible_version_fails_closed() -> None:
    checkpoint = serialize_workflow_state(_rich_state(), last_event_seq=1)
    checkpoint["version"] = CHECKPOINT_VERSION + 1
    with pytest.raises(CheckpointSchemaError):
        restore_workflow_state(checkpoint)


def test_langchain_messages_round_trip_through_json() -> None:
    """Runtime BaseMessage objects need conversion before JSON-backed
    persistence."""
    from langchain_core.messages import AIMessage, HumanMessage

    state = _rich_state()
    state["messages"] = [
        HumanMessage(content="goal"),
        AIMessage(content="hypothesis drafted"),
    ]

    checkpoint = serialize_workflow_state(state, last_event_seq=1)
    reloaded = json.loads(json.dumps(checkpoint))
    restored = restore_workflow_state(reloaded)

    messages = restored["messages"]
    assert [type(m).__name__ for m in messages] == [
        "HumanMessage",
        "AIMessage",
    ]
    assert [m.content for m in messages] == ["goal", "hypothesis drafted"]


def _envelope(*body: str) -> str:
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"])


def _apply(text: str, root: Path) -> Patch:
    patch = parse_patch(text)
    apply_patch(patch, root)
    return patch


def test_envelope_markers_are_required() -> None:
    with pytest.raises(PatchError, match="must start with"):
        parse_patch("*** Update File: a.py\n+x\n*** End Patch")
    with pytest.raises(PatchError, match="must end with"):
        parse_patch("*** Begin Patch\n*** Update File: a.py\n")


def test_multiple_files_parse_into_one_envelope() -> None:
    patch = parse_patch(
        _envelope(
            "*** Add File: new.py",
            "+print(1)",
            "*** Delete File: gone.py",
            "*** Update File: kept.py",
            "@@",
            " context",
            "-old",
            "+new",
        )
    )
    assert patch.paths == ("new.py", "gone.py", "kept.py")


def test_update_replaces_the_anchored_lines(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("before\nold\nafter\n")

    _apply(
        _envelope(
            "*** Update File: a.py",
            "@@",
            " before",
            "-old",
            "+new",
            " after",
        ),
        tmp_path,
    )

    assert target.read_text() == "before\nnew\nafter\n"


def test_add_refuses_to_clobber(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("existing\n")
    with pytest.raises(PatchError, match="already exists"):
        _apply(_envelope("*** Add File: a.py", "+new"), tmp_path)
    assert (tmp_path / "a.py").read_text() == "existing\n"


def test_a_stale_context_fails_rather_than_applying_elsewhere(
    tmp_path: Path,
) -> None:
    """Line numbers alone can target unrelated content after a file changes."""
    target = tmp_path / "a.py"
    target.write_text("completely\ndifferent\ncontent\n")

    with pytest.raises(PatchError, match="did not match"):
        _apply(
            _envelope("*** Update File: a.py", "@@", " expected", "-old", "+new"),
            tmp_path,
        )
    assert target.read_text() == "completely\ndifferent\ncontent\n"


def test_a_failing_patch_leaves_every_file_untouched(
    tmp_path: Path,
) -> None:
    """Plan all files before committing so a later failure cannot leave a
    partial edit."""
    good = tmp_path / "good.py"
    good.write_text("old\n")

    with pytest.raises(PatchError, match="does not exist"):
        _apply(
            _envelope(
                "*** Update File: good.py",
                "@@",
                "-old",
                "+new",
                "*** Update File: missing.py",
                "@@",
                "-a",
                "+b",
            ),
            tmp_path,
        )

    assert good.read_text() == "old\n"


def test_hunks_apply_in_order_within_one_file(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("dup\nmiddle\ndup\n")

    _apply(
        _envelope(
            "*** Update File: a.py",
            "@@",
            "-dup",
            "+first",
            "@@",
            "-dup",
            "+second",
        ),
        tmp_path,
    )

    assert target.read_text() == "first\nmiddle\nsecond\n"


@pytest.mark.parametrize("path", ["../escape.py", "sub/../../escape.py", "/etc/passwd"])
def test_paths_outside_the_root_are_refused(tmp_path: Path, path: str) -> None:
    with pytest.raises(PatchError, match=r"relative|outside"):
        _apply(_envelope(f"*** Add File: {path}", "+x"), tmp_path)


def test_a_symlinked_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)

    with pytest.raises(PatchError, match="outside"):
        _apply(_envelope("*** Add File: link/escape.py", "+x"), root)


def test_evolved_child_appends_without_replacing_parent() -> None:
    """Fresh children coexist with parents and must never resurrect pruned
    hypotheses."""
    parent = make_hypothesis("A hypothesis about kinase X")
    others = [make_hypothesis("B"), make_hypothesis("C")]
    existing = [parent, *others]
    child = Hypothesis(
        text="A refined hypothesis about kinase X and cofactor W",
        parent_id=parent.id,
        generation=1,
    )
    result = deduplicate_hypotheses(existing, AppendHypotheses([child]))
    ids = [h.id for h in result]
    assert parent.id in ids
    assert child.id in ids
    assert len(result) == 4
    surviving_parent = next(h for h in result if h.id == parent.id)
    assert surviving_parent.text == "A hypothesis about kinase X"


def _matchup(
    a_id: str, b_id: str, winner_before: int = 1200, loser_before: int = 1200
) -> dict[str, object]:
    return {
        "hypothesis_a_id": a_id,
        "hypothesis_b_id": b_id,
        "winner_elo_before": winner_before,
        "loser_elo_before": loser_before,
    }


def test_matchups_from_later_tournaments_do_not_erase_earlier_ones() -> None:
    """Last-write-wins would erase every earlier cycle of persisted Elo
    history."""
    from co_scientist.domains.research_state.state import accumulate_matchups

    first = [_matchup("a", "b")]
    second = [_matchup("c", "d")]

    combined = accumulate_matchups(first, second)

    assert combined == [_matchup("a", "b"), _matchup("c", "d")]


def test_a_replayed_task_does_not_double_its_own_ledger() -> None:
    """Research ledgers are content, not events; replay must not multiply
    them."""
    from co_scientist.domains.research_state.state import accumulate_research_ledgers

    ledger = {"goal": "reverse fibrosis", "calls": []}

    assert accumulate_research_ledgers([ledger], [dict(ledger)]) == [ledger]


@pytest.mark.parametrize(
    ("lines", "anchor", "start", "rung"),
    [
        # A scoring matcher would pick the best occurrence twice; the ladder
        # prefers an exact match anywhere over a looser earlier one.
        (["value ", "value"], "value", 1, "exact"),
        (["  code  "], "  code", 0, "trailing-whitespace"),
        (['x = "a"'], "x = “a”", 0, "unicode-punctuation"),
    ],
)
def test_anchor_ladder_prefers_the_strictest_rung(
    lines: list[str], anchor: str, start: int, rung: str
) -> None:
    found = seek_anchor(lines, (anchor,), 0)
    assert found is not None
    assert (found.start, found.rung) == (start, rung)


def test_repeated_anchors_resolve_in_order_and_missing_ones_do_not() -> None:
    lines = ["x", "dup", "y", "dup", "z"]
    first = seek_anchor(lines, ("dup",), 0)
    assert first is not None and first.start == 1
    second = seek_anchor(lines, ("dup",), first.end)
    assert second is not None and second.start == 3
    assert seek_anchor(lines, ("absent",), 0) is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "no file operations"),
        (["stray"], "expected a file operation"),
        (["*** Update File: a.py", "@@", "?bad marker"], "unrecognized line"),
        (["*** Update File: a.py"], "contains no changes"),
        (["*** Add File: a.py", "bad"], "added file must start with"),
        (["*** Delete File: absent.py"], "does not exist"),
    ],
)
def test_a_malformed_patch_is_refused_with_the_reason(
    tmp_path: Path, body: list[str], message: str
) -> None:
    with pytest.raises(PatchError, match=message):
        _apply(_envelope(*body), tmp_path)


def test_a_blank_context_line_and_the_eof_marker_are_tolerated(
    tmp_path: Path,
) -> None:
    """Trailing-whitespace stripping turns a blank context line from " " into
    "" somewhere between the model and the parser."""
    target = tmp_path / "a.py"
    target.write_text("keep\n\ndrop\n")
    patch = "\n" + _envelope(
        "*** Update File: a.py",
        "@@",
        " keep",
        "",
        "-drop",
        "+kept",
        "*** End of File",
    )
    _apply(patch, tmp_path)
    assert target.read_text() == "keep\n\nkept\n"
    assert seek_anchor(["a"], (), 3) is not None
    assert seek_anchor(["a"], ("a", "b"), 0) is None


def test_a_patch_adds_moves_and_deletes_files(tmp_path: Path) -> None:
    (tmp_path / "old.py").write_text("keep\nold\n")
    (tmp_path / "gone.py").write_text("x\n")

    _apply(
        _envelope(
            "*** Add File: sub/new.py",
            "+print(1)",
            "*** Delete File: gone.py",
            "*** Update File: old.py",
            "*** Move to: moved.py",
            "@@",
            " keep",
            "-old",
            "+new",
        ),
        tmp_path,
    )

    assert (tmp_path / "sub" / "new.py").read_text() == "print(1)\n"
    assert not (tmp_path / "gone.py").exists()
    assert not (tmp_path / "old.py").exists()
    assert (tmp_path / "moved.py").read_text() == "keep\nnew\n"


def test_replaying_or_skipping_a_ranking_update_keeps_the_history() -> None:
    """Last-write-wins would erase persisted Elo history, and a replayed
    commit must not double-count it."""
    first = [_matchup("a", "b")]
    second = [_matchup("c", "d")]

    assert accumulate_matchups(first, second) == first + second
    assert accumulate_matchups(first + second, list(second)) == first + second
    assert accumulate_matchups(first, []) == first


def test_state_pool_reducer_replaces_or_appends_without_duplicates() -> None:
    a, b = make_hypothesis("A"), make_hypothesis("B")
    a_rescored = Hypothesis(text="A rescored", id=a.id, score=2.0)

    replaced = deduplicate_hypotheses([a, b], [b, a, a_rescored])
    assert [h.id for h in replaced] == [b.id, a.id]
    assert replaced[1].score == a.score

    appended = deduplicate_hypotheses(
        [a], AppendHypotheses([a, make_hypothesis(" a "), make_hypothesis("C")])
    )
    assert [h.text for h in appended] == ["A", "C"]
    assert deduplicate_hypotheses([a], AppendHypotheses([])) == [a]
