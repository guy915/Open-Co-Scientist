from __future__ import annotations

import json
from typing import Any

import pytest
from co_scientist.api.runs.chat import _gather_qa_context
from co_scientist.domains.chat.qa import artifacts, build_system_prompt
from co_scientist.domains.chat.repository import interviews
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunRow, RunStatus

from tests._store_helpers import seed_checkpoint, seed_run


def _run(config: dict[str, Any] | None = None) -> RunRow:
    run = seed_run(
        "Test scientific goal",
        profile="express",
        provider="mock",
        config=config or {},
        client_id="owner",
    )
    runs.update_run_status(run.id, RunStatus.RUNNING)
    result = runs.get_run(run.id)
    assert result is not None
    return result


def _checkpoint(run: Any, state: dict[str, Any], version: int = 1) -> None:
    seed_checkpoint(
        run.id,
        {"version": version, "state": state},
        stage="reflection",
        schema_version=version,
    )


@pytest.mark.parametrize("reason_key", ["reasoning", "rationale"])
def test_mid_run_reads_committed_science_without_draining_or_writing(
    monkeypatch: pytest.MonkeyPatch,
    reason_key: str,
) -> None:
    run = _run()
    state = {
        "hypotheses": [
            {
                "id": "h1",
                "title": "Lipid repair",
                "text": "Block lipid repair",
                "deep_verification_verdict": "unproven",
                "reviews": [
                    {
                        "review_summary": "Needs replication",
                        "reviewer": "reflection",
                    }
                ],
            }
        ],
        "articles": [{"title": "Full literature", "abstract": "Grounding text"}],
        "tournament_matchups": [{"winner_id": "h1", reason_key: "Better controlled"}],
        "meta_review": {"conclusion": "Still preliminary"},
        "supervisor_guidance": {"goal": "Test replication"},
        "constraints": ["Human cells"],
        "model_name": "secret runtime route",
    }
    _checkpoint(run, state)
    assert hypotheses.list_hypotheses(run.id) == []
    original = db.connect
    writes: list[str] = []
    from contextlib import contextmanager

    @contextmanager
    def readonly() -> Any:
        with original() as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.set_trace_callback(writes.append)
            yield conn

    monkeypatch.setattr(db, "connect", readonly)
    context = _gather_qa_context(run)
    assert context.hypotheses[0]["statement"] == "Block lipid repair"
    assert context.hypotheses[0]["verification_verdict"] == "unproven"
    assert context.progress and context.progress.idea_count == 1
    assert context.reviews[0]["summary"] == "Needs replication"
    assert context.matches[0]["rationale"] == "Better controlled"
    assert "Better controlled" in build_system_prompt(context)
    assert context.manifest == []  # checkpoint literature is not verified
    for section in (
        "articles",
        "meta_review",
        "supervisor_guidance",
        "constraints",
    ):
        result = json.loads(artifacts.retrieve(context.artifacts, {"section": section}))
        assert result["records"]
    assert "secret runtime route" not in json.dumps(context.artifacts)
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in writes
    )


def test_interview_owner_is_checked_and_all_answers_are_retrievable() -> None:
    interview = interviews.create_interview("owner", "Initial challenge")
    run = _run(
        {
            "interview_id": interview["id"],
            "requirements": ["No animal work"],
            "setup": {"goal": "Study repair", "criteria": ["Replicable"]},
        }
    )
    context = _gather_qa_context(run)
    assert context.artifacts["interview"][1]["content"] == "Initial challenge"
    assert context.artifacts["setup"][1]["requirements"] == ["No animal work"]
    assert context.artifacts["setup"][0]["criteria"] == ["Replicable"]
    run.client_id = "other"
    assert "interview" not in _gather_qa_context(run).artifacts
