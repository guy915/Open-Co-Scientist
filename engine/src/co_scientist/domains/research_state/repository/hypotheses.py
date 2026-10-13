from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from co_scientist.domains.research_state.elo import INITIAL_ELO
from co_scientist.domains.research_state.hypothesis_fields import HYPOTHESIS_FIELDS
from co_scientist.platform.db import current_time, use_conn


@dataclass(frozen=True)
class NewHypothesis:
    """Scientist contributions have no creation cycle; proposer toxicity
    assessments never substitute for safety screening.
    """

    run_id: str
    title: str
    statement: str
    hypothesis_id: str | None = None
    parent_id: str | None = None
    parent_ids: list[str] | None = None
    generation: int = 0
    creation_iteration: int | None = None
    category: str | None = None
    mechanism: str = ""
    expected_effect: str = ""
    experimental_context: str = ""
    introduction: str = ""
    recent_findings: str = ""
    safety_and_toxicity: str = ""
    created_by_agent: str = "generation"
    author: str = ""


def _parent_ids_json(parent_ids: list[str] | None) -> str | None:
    if not parent_ids:
        return None
    return json.dumps(parent_ids)


# Drains replay scientist contributions; keep stored identity and text on
# conflict, filling only missing engine-derived details.
_HYPOTHESIS_UPSERT = (
    "INSERT INTO hypotheses (id, run_id, parent_id, parent_ids, "
    "generation, creation_iteration, category, title, statement, mechanism, "
    "expected_effect, "
    "experimental_context, introduction, recent_findings, "
    "safety_and_toxicity, created_by_agent, author, created_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "creation_iteration=COALESCE("
    "hypotheses.creation_iteration, excluded.creation_iteration), "
    "category=COALESCE(hypotheses.category, excluded.category), "
    "mechanism=COALESCE(NULLIF(hypotheses.mechanism, ''), "
    "excluded.mechanism), "
    "expected_effect=COALESCE(NULLIF(hypotheses.expected_effect, ''), "
    "excluded.expected_effect), "
    "experimental_context=COALESCE("
    "NULLIF(hypotheses.experimental_context, ''), "
    "excluded.experimental_context), "
    "introduction=COALESCE(NULLIF(hypotheses.introduction, ''), "
    "excluded.introduction), "
    "recent_findings=COALESCE(NULLIF(hypotheses.recent_findings, ''), "
    "excluded.recent_findings), "
    "safety_and_toxicity=COALESCE("
    "NULLIF(hypotheses.safety_and_toxicity, ''), "
    "excluded.safety_and_toxicity)"
)

# Re-persistence must retain screening status and accumulated tournament
# counters rather than recreating mutable state.
_HYPOTHESIS_STATE_INSERT = (
    "INSERT INTO hypothesis_state (hypothesis_id, elo_rating, updated_at) "
    "VALUES (?,?,?) ON CONFLICT(hypothesis_id) DO NOTHING"
)


def add_hypothesis(
    hypothesis: NewHypothesis,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    hyp_id = hypothesis.hypothesis_id or str(uuid.uuid4())
    with use_conn(conn, db_path) as conn:
        now = current_time()
        conn.execute(
            _HYPOTHESIS_UPSERT,
            (
                hyp_id,
                hypothesis.run_id,
                hypothesis.parent_id,
                _parent_ids_json(hypothesis.parent_ids),
                hypothesis.generation,
                hypothesis.creation_iteration,
                hypothesis.category,
                hypothesis.title,
                hypothesis.statement,
                hypothesis.mechanism,
                hypothesis.expected_effect,
                hypothesis.experimental_context,
                hypothesis.introduction,
                hypothesis.recent_findings,
                hypothesis.safety_and_toxicity,
                hypothesis.created_by_agent,
                hypothesis.author,
                now,
            ),
        )
        conn.execute(_HYPOTHESIS_STATE_INSERT, (hyp_id, INITIAL_ELO, now))
    return hyp_id


@dataclass(frozen=True)
class HypothesisStateChanges:
    elo_rating: int | None = None
    win_delta: int = 0
    loss_delta: int = 0
    novelty: float | None = None
    clear_novelty: bool = False
    cluster_id: str | None = None
    safety_status: str | None = None
    status: str | None = None
    verification_verdict: str | None = None


def _hypothesis_state_updates(
    changes: HypothesisStateChanges,
) -> list[tuple[str, Any]]:
    """Relative counter updates prevent concurrent match writers from
    clobbering one another.
    """
    c = changes
    candidates: tuple[tuple[bool, str, Any], ...] = (
        (c.elo_rating is not None, "elo_rating=?", c.elo_rating),
        (bool(c.win_delta), "win_count=win_count+?", c.win_delta),
        (bool(c.loss_delta), "loss_count=loss_count+?", c.loss_delta),
        (
            c.novelty is not None or c.clear_novelty,
            "novelty_score=?",
            None if c.clear_novelty else c.novelty,
        ),
        (c.cluster_id is not None, "cluster_id=?", c.cluster_id),
        (c.safety_status is not None, "safety_status=?", c.safety_status),
        (c.status is not None, "status=?", c.status),
        (
            c.verification_verdict is not None,
            "verification_verdict=?",
            c.verification_verdict,
        ),
    )
    return [(fragment, value) for active, fragment, value in candidates if active]


def update_hypothesis_state(
    hypothesis_id: str,
    changes: HypothesisStateChanges,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    updates = _hypothesis_state_updates(changes)
    with use_conn(conn, db_path) as conn:
        sets = [fragment for fragment, _ in updates] + ["updated_at=?"]
        params: list[Any] = [value for _, value in updates] + [
            current_time(),
            hypothesis_id,
        ]
        set_clause = ", ".join(sets)
        conn.execute(
            f"UPDATE hypothesis_state SET {set_clause} WHERE hypothesis_id=?",
            params,
        )


# Safety redaction is the sole sanctioned mutation of otherwise append-only
# hypothesis text.
_REDACTABLE_COLUMNS = frozenset(field.store for field in HYPOTHESIS_FIELDS if field.redacted)


def redact_hypothesis_fields(
    hypothesis_id: str,
    fields: dict[str, str],
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    unknown = set(fields) - _REDACTABLE_COLUMNS
    if unknown:
        raise ValueError(f"non-redactable hypothesis columns: {sorted(unknown)}")
    if not fields:
        return
    # SQL identifiers come only from the literal allowlist; user text stays in
    # bound values.
    set_clause = ", ".join(f"{column}=?" for column in sorted(fields))
    params = [fields[column] for column in sorted(fields)]
    with use_conn(conn, db_path) as conn:
        conn.execute(
            f"UPDATE hypotheses SET {set_clause} WHERE id=?",
            (*params, hypothesis_id),
        )


# Tournament counters are non-null by projection; unreviewed scores and other
# optional state remain nullable.
_HYP_SELECT = (
    f"SELECT h.*, COALESCE(s.elo_rating, {INITIAL_ELO}) AS elo_rating, "
    "COALESCE(s.win_count, 0) AS win_count, "
    "COALESCE(s.loss_count, 0) AS loss_count, "
    "s.novelty_score, s.plausibility_score, "
    "s.testability_score, s.safety_status, s.status, s.cluster_id, "
    "s.verification_verdict "
    "FROM hypotheses h LEFT JOIN hypothesis_state s ON h.id=s.hypothesis_id "
)


def _decode_parent_ids(row: dict[str, Any]) -> dict[str, Any]:
    """Legacy NULL or malformed lineage degrades to None rather than failing
    report reads.
    """
    raw = row.get("parent_ids")
    if raw is None:
        return row
    try:
        row["parent_ids"] = json.loads(raw)
    except (TypeError, ValueError):
        row["parent_ids"] = None
    return row


def list_hypotheses(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    with use_conn(conn, db_path) as conn:
        # API and Q&A consumers rely on descending Elo with deterministic
        # creation-time ties.
        rows = conn.execute(
            _HYP_SELECT + "WHERE h.run_id=? ORDER BY s.elo_rating DESC, h.created_at ASC",
            (run_id,),
        ).fetchall()
        return [_decode_parent_ids(dict(r)) for r in rows]
