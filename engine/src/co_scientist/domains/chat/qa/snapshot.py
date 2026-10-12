"""Chat views committed checkpoints without restoring or draining the engine."""

from __future__ import annotations

import sqlite3
from typing import Any

from co_scientist.domains.chat.repository import interviews
from co_scientist.domains.chat.repository.example_summaries import get_display_report
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state.hypothesis_fields import store_text_fields
from co_scientist.domains.research_state.repository import hypotheses as store_hypotheses
from co_scientist.domains.research_state.repository import records
from co_scientist.platform.db import checkpoints, supervisor_plan
from co_scientist.platform.db.checkpoints import CHECKPOINT_VERSION
from co_scientist.platform.db.models import RunRow

# Exclude runtime handles, credentials and model routing.
_SCIENCE_KEYS = (
    "research_goal",
    "preferences",
    "attributes",
    "constraints",
    "lab_constraints",
    "criteria",
    "run_focus_guidance",
    "run_setup_guidance",
    "starting_hypotheses",
    "literature",
    "articles_with_reasoning",
    "literature_review_queries",
    "articles",
    "meta_review",
    "research_overview",
    "interim_overview",
    "research_ledgers",
    "research_expansion_findings",
    "context_enrichment_sources",
    "debate_transcripts",
    "evolution_details",
    "removed_duplicates",
    "proximity_graph",
    "safety_decisions",
    "safety_blocked",
    "held_for_review",
    "degraded_nodes",
    "retrieval_degradation",
)


def _items(raw: Any) -> list[Any]:
    return raw if isinstance(raw, list) else [raw] if raw is not None else []


def checkpoint_state(run: RunRow, conn: sqlite3.Connection) -> dict[str, Any]:
    latest = checkpoints.get_latest_checkpoint(run.id, conn=conn)
    envelope = latest.get("state") if latest else None
    if not isinstance(envelope, dict) or envelope.get("version") != CHECKPOINT_VERSION:
        return {}
    state = envelope.get("state")
    return state if isinstance(state, dict) else {}


def idea_view(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        **raw,
        "title": raw.get("title") or str(raw.get("text") or "Untitled")[:160],
        **store_text_fields(raw),
        "status": raw.get("review_disposition")
        or ("reviewed" if raw.get("reviews") else "unreviewed"),
        "verification_verdict": raw.get("deep_verification_verdict"),
    }


def gather_artifacts(
    run: RunRow,
    conn: sqlite3.Connection,
    state: dict[str, Any],
    hypotheses: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    history: list[Any],
) -> dict[str, list[Any]]:
    artifacts: dict[str, list[Any]] = {
        "goal": [run.research_goal],
        "setup": [
            run.config.get("setup") or {},
            {
                key: value
                for key, value in run.config.items()
                if key
                in (
                    *_SCIENCE_KEYS,
                    "requirements",
                    "focus",
                    "tier",
                    "run_mode",
                    "budget",
                    "limits",
                )
            },
        ],
        "checkpoint_hypotheses": _items(state.get("hypotheses")),
        "supervisor_guidance": _items(state.get("supervisor_guidance")),
        "plan": [supervisor_plan.get_supervisor_plan(run.id, conn=conn) or {}],
        "hypotheses": hypotheses,
        "reviews": reviews,
        "published_reviews": records.list_reviews(run.id, conn=conn),
        "published_hypotheses": store_hypotheses.list_hypotheses(run.id, conn=conn),
        "matches": matches,
        "checkpoint_matches": _items(state.get("tournament_matchups")),
        "conversation": [m.to_dict() for m in history],
        "evidence": records.list_evidence(run.id, conn=conn),
        "citations": records.list_citations(run.id, conn=conn),
        "claim_evidence": records.list_claim_evidence(run.id, conn=conn),
        "safety": records.list_safety_decisions(run.id, conn=conn),
        "knowledge_facts": reports.list_knowledge_facts(run.id, conn=conn),
    }
    for key in _SCIENCE_KEYS:
        if state.get(key) is not None:
            artifacts[key] = _items(state[key])
    interview_id = run.config.get("interview_id")
    interview = interviews.get_interview(str(interview_id), conn=conn) if interview_id else None
    if interview and interview["client_id"] == run.client_id:
        artifacts["interview"] = [interview["fields"], *interview["turns"]]
    latest = get_display_report(run.id, conn=conn)
    if latest:
        artifacts["report"] = [latest["payload"], latest["markdown_text"]]
    return artifacts
