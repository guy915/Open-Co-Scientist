from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluations._identity import identity_digest, validate_identity
from evaluations._usage_evidence import summarize_usage
from evaluations.quality_goals import GOALS


@dataclass(frozen=True)
class Snapshot:
    goal_id: str
    report: str
    metrics: dict[str, Any]
    identity: dict[str, Any]


def _report_ideas(markdown: str, hypotheses: list[sqlite3.Row]) -> dict[str, list[str]]:
    # Count full idea entries, not title mentions in directions, standings or references.
    sections = re.split(r"(?m)^### (?=\d+\.)", markdown)
    delivered: dict[str, list[str]] = {"featured": [], "screened": []}
    for hyp in hypotheses:
        matches = [
            s.split("\n## ", 1)[0]
            for s in sections[1:]
            if _heading_title(s.splitlines()[0]) == hyp["title"]
        ]
        if len(matches) > 1:
            raise ValueError("ambiguous report idea headings")
        if matches:
            if sum(other["title"] == hyp["title"] for other in hypotheses) != 1:
                raise ValueError("ambiguous delivered hypothesis identity")
            category = (
                "screened" if "screened, not deep-verified" in matches[0].lower() else "featured"
            )
            delivered[category].append(str(hyp["id"]))
    return delivered


def _heading_title(heading: str) -> str:
    heading = re.sub(r"^\d+\.\s*", "", heading)
    heading = heading.split("**", 2)[1] if "**" in heading else heading
    return re.sub(r"^(?:Open )?Co-Scientist - ", "", heading).strip()


def _usage(metrics: dict[str, Any], attempted: int | None) -> dict[str, Any]:
    usage = metrics.get("model_usage") or {}
    evidence = summarize_usage(usage)
    complete = evidence["has_usage_records"] and not evidence["unreported_usage_calls"]
    if attempted is not None and attempted != evidence["physical_calls"]:
        complete = False
    prompt = sum(int(row.get("prompt_tokens", 0)) for row in usage.values())
    completion = sum(int(row.get("completion_tokens", 0)) for row in usage.values())
    return {
        "physical_calls": attempted
        if attempted is not None
        else (evidence["physical_calls"] if usage else None),
        "call_count_basis": "benchmark_dispatch_counter"
        if attempted is not None
        else "recorded_telemetry_only",
        "prompt_tokens": prompt if complete else None,
        "completion_tokens": completion if complete else None,
        # Reasoning is normally already included in completion, so never add it twice.
        "total_tokens": prompt + completion if complete else None,
        "reported_token_subtotal": prompt + completion,
        "usage_evidence": evidence,
    }


def read_snapshot(db: Path, run_id: str, goal_id: str, source_commit: str) -> Snapshot:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source_commit must be a resolved 40-character git SHA")
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run["research_goal"] != GOALS[goal_id]:
            raise ValueError("run does not match the fixed goal")
        config = json.loads(run["config_json"])
        identity = validate_identity(config.get("evaluation_identity"))
        if identity["goal_sha256"] != identity_digest_goal(GOALS[goal_id]):
            raise ValueError("stored identity does not match goal")
        actual_config = {
            key: value for key, value in config.items() if key != "evaluation_identity"
        }
        if identity.get("resolved_config") != actual_config:
            raise ValueError("stored run config changed after its identity snapshot")
        if identity.get("backend") != run["llm_backend"]:
            raise ValueError("stored backend differs from its identity snapshot")
        report_row = conn.execute(
            "SELECT * FROM reports WHERE run_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        markdown = str(report_row["markdown_text"] or "") if report_row else ""
        hyps = conn.execute(
            "SELECT h.id, h.title, s.verification_verdict FROM hypotheses h "
            "LEFT JOIN hypothesis_state s ON s.hypothesis_id=h.id WHERE h.run_id=?",
            (run_id,),
        ).fetchall()
        ideas = _report_ideas(markdown, hyps)
        claims = conn.execute(
            "SELECT hypothesis_id, claim, label FROM claim_evidence WHERE run_id=?", (run_id,)
        ).fetchall()
        row = conn.execute(
            "SELECT metrics_json FROM run_metrics WHERE run_id=?", (run_id,)
        ).fetchone()
        metrics = json.loads(row[0]) if row else {}
        attempted, counter_kind, counter_sha = _dispatch_count(conn, run_id, source_commit)
        times = conn.execute(
            "SELECT MIN(started_at), MAX(completed_at) FROM scientific_tasks WHERE run_id=?",
            (run_id,),
        ).fetchone()
    ids = set(ideas["featured"] + ideas["screened"])
    supported = {
        (c["hypothesis_id"], c["claim"]) for c in claims if c["label"] in {"supports", "partial"}
    }
    wall = times[1] - times[0] if times[0] is not None and times[1] is not None else None
    usage = _usage(metrics, attempted)
    if counter_kind == "backend_invocations" and run["llm_backend"] == "real":
        usage.update(
            physical_calls=None, prompt_tokens=None, completion_tokens=None, total_tokens=None
        )
    if counter_kind is not None:
        usage["call_count_basis"] = counter_kind
    facts = {
        "run_id": run_id,
        "source_commit": source_commit,
        "tier": run["profile"],
        "backend": run["llm_backend"],
        "completed": run["status"] == "completed" and bool(markdown.strip()),
        "status": run["status"],
        "report_sha256": identity_digest(markdown),
        "ideas_generated": len(hyps),
        "ideas_featured": len(ideas["featured"]),
        "ideas_screened": len(ideas["screened"]),
        "ideas_delivered": len(ids),
        "delivered_ids": sorted(ids),
        "verification_verdict_mix": dict(
            Counter(h["verification_verdict"] or "not_assessed" for h in hyps)
        ),
        "delivered_verdict_mix": dict(
            Counter(h["verification_verdict"] or "not_assessed" for h in hyps if h["id"] in ids)
        ),
        "claim_verdict_mix": dict(Counter(c["label"] for c in claims)),
        "claims_assessed": len({(c["hypothesis_id"], c["claim"]) for c in claims}),
        "supported_claims": len(supported),
        "delivered_supported_claims": sum(hid in ids for hid, _ in supported),
        "wall_seconds": round(wall, 3) if wall is not None else None,
        **usage,
        "recorded_backend_invocations": attempted
        if counter_kind == "backend_invocations"
        else None,
        "request_counter_sha256": counter_sha,
    }
    return Snapshot(goal_id, markdown, facts, identity)


def _dispatch_count(
    conn: sqlite3.Connection,
    run_id: str,
    source_commit: str,
) -> tuple[int | None, str | None, str | None]:
    # Older saved databases predate the evaluation-only dispatch receipt.
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evaluation_runs'"
    ).fetchone():
        return None, None, None
    columns = {r[1] for r in conn.execute("PRAGMA table_info(evaluation_runs)")}
    versioned = {"counter_kind", "request_counter_sha256"}.issubset(columns)
    row = conn.execute(
        "SELECT source_commit, physical_requests"
        + (", counter_kind, request_counter_sha256" if versioned else "")
        + " FROM evaluation_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None, None, None
    if row[0] != source_commit:
        raise ValueError("source commit differs from the benchmark receipt")
    if type(row[1]) is not int or row[1] < 0:
        raise ValueError("invalid benchmark request counter")
    if not versioned:
        return row[1], "backend_invocations", None
    if row[2] != "http_transport_attempts" or not re.fullmatch(r"[0-9a-f]{64}", row[3] or ""):
        raise ValueError("invalid HTTP attempt counter identity")
    return row[1], row[2], row[3]


def identity_digest_goal(goal: str) -> str:
    import hashlib

    return hashlib.sha256(goal.encode()).hexdigest()
