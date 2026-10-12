SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_markers (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, event_time INTEGER NOT NULL,
    next_attempt REAL NOT NULL, attempts INTEGER NOT NULL, delivered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_alert_markers_pending
    ON alert_markers(next_attempt) WHERE delivered_at IS NULL;
CREATE TABLE IF NOT EXISTS launch_control (
    id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL, drain INTEGER NOT NULL,
    message TEXT NOT NULL, resumes_at REAL, revision INTEGER NOT NULL,
    drain_generation INTEGER NOT NULL
);
-- Admission ledgers are independent of run deletion and survive restarts.
CREATE TABLE IF NOT EXISTS anonymous_admissions (
    day INTEGER NOT NULL, host TEXT NOT NULL, client_id TEXT NOT NULL,
    PRIMARY KEY(day, host, client_id)
);
CREATE TABLE IF NOT EXISTS run_admissions (
    run_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, host TEXT NOT NULL,
    day INTEGER NOT NULL, free INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_admissions_day_host ON run_admissions(day, host);
CREATE TABLE IF NOT EXISTS provider_admissions (
    day INTEGER NOT NULL, scope TEXT NOT NULL, subject TEXT NOT NULL,
    calls INTEGER NOT NULL, tokens INTEGER NOT NULL,
    PRIMARY KEY(day, scope, subject)
);
CREATE TABLE IF NOT EXISTS provider_token_reservations (
    id TEXT PRIMARY KEY, day INTEGER NOT NULL, client_id TEXT NOT NULL,
    host TEXT NOT NULL, tokens INTEGER NOT NULL, app INTEGER NOT NULL,
    used_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS llm_spend (
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, model TEXT NOT NULL, role TEXT NOT NULL,
    reserved_microeur INTEGER NOT NULL CHECK(reserved_microeur >= 0),
    charged_microeur INTEGER NOT NULL
        CHECK(charged_microeur >= 0 AND charged_microeur <= reserved_microeur),
    input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL, rates TEXT NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER, output_tokens INTEGER,
    cached_tokens INTEGER, cache_write_tokens INTEGER, refused INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_spend_created_at ON llm_spend(created_at);
CREATE TABLE IF NOT EXISTS llm_spend_holds (reason TEXT PRIMARY KEY);
-- Only an operator creates a version; a store without one refuses Azure, so a
-- lost volume or a second process never starts again from a full allowance.
CREATE TABLE IF NOT EXISTS llm_azure_allowance (
    version INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
    allowance_microeur INTEGER NOT NULL CHECK(allowance_microeur > 0),
    expires_at REAL NOT NULL, cutoff_at REAL NOT NULL CHECK(cutoff_at < expires_at),
    rates TEXT NOT NULL, basis TEXT NOT NULL,
    holds_through INTEGER NOT NULL DEFAULT 0 CHECK(holds_through >= 0)
);
CREATE TRIGGER IF NOT EXISTS llm_azure_allowance_append_only
    BEFORE UPDATE ON llm_azure_allowance
    BEGIN SELECT RAISE(ABORT, 'Azure allowance history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_azure_allowance_no_delete
    BEFORE DELETE ON llm_azure_allowance
    BEGIN SELECT RAISE(ABORT, 'Azure allowance history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_spend_no_delete BEFORE DELETE ON llm_spend
    BEGIN SELECT RAISE(ABORT, 'Spend ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS llm_spend_settles_once BEFORE UPDATE ON llm_spend
    WHEN OLD.settled != 0 OR NEW.settled != 1 OR NEW.reserved_microeur != OLD.reserved_microeur
    BEGIN SELECT RAISE(ABORT, 'Spend reservations settle once'); END;
CREATE TRIGGER IF NOT EXISTS llm_spend_holds_no_delete BEFORE DELETE ON llm_spend_holds
    BEGIN SELECT RAISE(ABORT, 'Spend holds need manual store repair'); END;
CREATE TABLE IF NOT EXISTS anthropic_credit_cycles (
    start REAL PRIMARY KEY, end REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS anthropic_credit_state (
    slot TEXT PRIMARY KEY, disabled_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS anthropic_credit (
    id TEXT PRIMARY KEY, cycle_start REAL NOT NULL, created_at REAL NOT NULL,
    role TEXT NOT NULL, reserved_microusd INTEGER NOT NULL CHECK(reserved_microusd >= 0),
    charged_microusd INTEGER NOT NULL
        CHECK(charged_microusd >= 0 AND charged_microusd <= reserved_microusd),
    settled INTEGER NOT NULL DEFAULT 0, input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL,
    rates TEXT NOT NULL,
    prompt_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, cache_write_tokens INTEGER,
    refused INTEGER
);
CREATE INDEX IF NOT EXISTS idx_anthropic_credit_cycle ON anthropic_credit(cycle_start);
CREATE TABLE IF NOT EXISTS llm_routes (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    slot TEXT NOT NULL, azure_allowed INTEGER NOT NULL,
    forecast_microeur INTEGER NOT NULL CHECK(forecast_microeur >= 0)
);
CREATE TABLE IF NOT EXISTS llm_route_blocks (
    slot TEXT PRIMARY KEY, unavailable_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_free_calls (
    day INTEGER PRIMARY KEY, calls INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_forecast_allocations (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES llm_routes(run_id) ON DELETE CASCADE,
    converted_microeur INTEGER NOT NULL CHECK(converted_microeur >= 0)
);
CREATE TABLE IF NOT EXISTS run_call_admissions (
    run_id TEXT PRIMARY KEY, calls INTEGER NOT NULL, ceiling INTEGER NOT NULL
);
-- Explicitly retired empirical-outcomes data; no runtime consumer remains.
DROP TABLE IF EXISTS outcome_refinement_actions;
DROP TABLE IF EXISTS hypothesis_outcomes;
DROP TABLE IF EXISTS report_shares;
-- Primary lifecycle record for a single hypothesis-generation run.
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    research_goal TEXT NOT NULL,
    -- short model-generated session heading, distinct from research_goal;
    -- NULL until generated (surfaces fall back to a clause of the goal)
    title TEXT,
    -- canonical run mode; column name kept for legacy clients
    profile TEXT NOT NULL,
    -- draft|queued|running|synthesizing|completed|failed|blocked|cancelled
    status TEXT NOT NULL,
    provider TEXT NOT NULL,          -- 'mock' | 'engine'
    -- JSON: initial_count, iterations, evolution_count, k_factor, ...
    config_json TEXT NOT NULL,
    client_id TEXT NOT NULL DEFAULT '',
    execution_policy TEXT NOT NULL DEFAULT 'standard',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    error TEXT,
    llm_backend TEXT,                -- 'offline' | 'real'
    -- freshly synthesized narrative restatement of the goal in different
    -- words (GOAL-RESTATEMENT-001); NULL until a background generator fills
    -- it, and NULL on offline/keyless runs and rows predating this column
    goal_restatement TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
-- Owner listings filter by client and order by recency.
CREATE INDEX IF NOT EXISTS idx_runs_client_created ON runs(client_id, created_at DESC);

-- Bring-your-own-key credentials (see app/credentials.py). One encrypted
-- credential per run, persisted for the run's lifetime because a run's
-- durable tasks lease independently and may execute later or elsewhere
-- than the request that created the run. `encrypted_key` holds a Fernet
-- token -- never plaintext -- and the row cascades away with its run, so
-- deleting a run deletes its key. `client_id` records the owning
-- identity the key was accepted from.
CREATE TABLE IF NOT EXISTS run_credentials (
    run_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    provider TEXT NOT NULL,          -- one of config.PROVIDER_CREDENTIAL_ENV
    model TEXT NOT NULL,             -- litellm worker-tier model
    supervisor_model TEXT,           -- supervisor tier; NULL = model
    encrypted_key TEXT NOT NULL,     -- Fernet token, never plaintext
    created_at REAL NOT NULL,
    supervisor_provider TEXT,        -- NULL = supervisor shares worker key
    encrypted_supervisor_key TEXT,   -- Fernet token for that provider
    custom_models_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- Free-use slots have no run FK: deletion must not refund the allowance.
CREATE TABLE IF NOT EXISTS free_run_usage (
    run_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_free_run_usage_client
    ON free_run_usage(client_id, created_at);

-- Owner-scoped receipts make ambiguous run-create retries replay one run.
-- The digest contains only canonical intent and a keyed fingerprint of any
-- explicit BYOK secret; the raw provider key is never stored here.
CREATE TABLE IF NOT EXISTS run_creation_receipts (
    client_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    PRIMARY KEY (client_id, idempotency_key),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- Durable pre-run Agent interview. The structured fields are derived from the
-- append-only turn transcript and remain editable until finalized.
CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    execution_policy TEXT NOT NULL DEFAULT 'standard',
    status TEXT NOT NULL,             -- active | completed | cancelled
    fields_json TEXT NOT NULL,
    current_question TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_interviews_client
    ON interviews(client_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS interview_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id TEXT NOT NULL,
    role TEXT NOT NULL,               -- user | agent
    content TEXT NOT NULL,
    -- the Agent's chain of thought for this turn; NULL for user turns and
    -- for models that emit none
    reasoning TEXT,
    -- 1 when the deterministic recovery path authored this Agent turn
    -- because no model could be reached (see
    -- interviews.model._fallback_interview_response); 0 for model-driven
    -- turns and every user turn. Per turn, so a mid-session credential
    -- change marks only the turns it affects.
    fallback INTEGER NOT NULL DEFAULT 0,
    -- JSON array of the structured multiple-choice questions this Agent turn
    -- offered the scientist (see interviews/questions.py). NULL for user
    -- turns and for any turn that asked nothing choosable. Per turn, never
    -- cumulative: a question belongs to the turn that asked it, so a reopened
    -- chat re-offers only the one still awaiting an answer.
    questions_json TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (interview_id) REFERENCES interviews(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_interview_turns
    ON interview_turns(interview_id, id ASC);

-- Scientist documents uploaded BEFORE any run exists, so an attachment can
-- ground the interview that scopes the goal and can be carried into the run
-- as part of creating it. Owned by client_id and never read across owners.
-- `interview_id` is set when the document is attached to a chat, `run_id`
-- when creating a run copies it into that run's private corpus; a row keeps
-- both so a document is traceable from chat to run. Deliberately not
-- foreign-keyed: a document exists before either row does.
CREATE TABLE IF NOT EXISTS staged_documents (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    interview_id TEXT,
    run_id TEXT,
    title TEXT NOT NULL,
    text TEXT NOT NULL,               -- extracted text, never the raw bytes
    mime_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    extraction_tool TEXT NOT NULL,
    created_at REAL NOT NULL,
    peer_hash TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_staged_documents_client
    ON staged_documents(client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_staged_documents_interview
    ON staged_documents(interview_id, created_at ASC);

CREATE TABLE IF NOT EXISTS input_admissions (
    day INTEGER NOT NULL,
    scope TEXT NOT NULL,
    subject TEXT NOT NULL,
    requests INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY(day, scope, subject)
);

-- Append-only timeline of everything that happened during a run. This is the
-- canonical source the SSE endpoint replays on client reconnect or restart.
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,            -- per-run monotonic sequence number
    -- agent name, 'status', 'log', 'metric', ...
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON run_events(run_id, seq);


-- Append-only hypothesis records: `evolve` inserts a new row with parent_id
-- set rather than mutating the parent. Mutable fields (Elo, scores, status)
-- live in hypothesis_state below, keyed by hypothesis id.
CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    parent_id TEXT,                  -- NULL for generation-0; set by evolve
    -- JSON array of every parent id for multi-parent combination children;
    -- NULL when a single parent_id is the whole lineage. parent_id stays the
    -- primary parent so existing lineage consumers are unaffected.
    parent_ids TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    -- Authoring-cycle ordinal the engine stamps at creation (0 for the
    -- initial generation, N for a research-expansion/evolution cycle). The
    -- run's true timeline axis; NULL on legacy rows. See EVAL-SCALING-001.
    creation_iteration INTEGER,
    category TEXT,                   -- short classification label (breadcrumb)
    title TEXT NOT NULL,
    statement TEXT NOT NULL,
    mechanism TEXT,
    expected_effect TEXT,
    experimental_context TEXT,
    created_by_agent TEXT NOT NULL,  -- 'generation' | 'evolution'
    created_at REAL NOT NULL,
    author TEXT,
    introduction TEXT,
    recent_findings TEXT,
    safety_and_toxicity TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES hypotheses(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_hyp_run ON hypotheses(run_id);
CREATE INDEX IF NOT EXISTS idx_hyp_parent ON hypotheses(parent_id);

-- Mutable state for a hypothesis (Elo, scores, status). Kept separate from the
-- append-only `hypotheses` table so the original record is never overwritten.
CREATE TABLE IF NOT EXISTS hypothesis_state (
    hypothesis_id TEXT PRIMARY KEY,
    -- DEFAULT mirrors co_scientist.domains.research_state.elo.INITIAL_ELO; rows are
    -- always inserted with an explicit rating, so this is only a belt-and-suspenders
    -- fallback.
    elo_rating INTEGER NOT NULL DEFAULT 1200,
    win_count INTEGER NOT NULL DEFAULT 0,
    loss_count INTEGER NOT NULL DEFAULT 0,
    novelty_score REAL,
    -- plausibility_score/testability_score/safety_status/status are reserved
    -- columns: update_hypothesis_state does not currently set them, and the
    -- underlying scores live on reviews instead (see reviews table below).
    plausibility_score REAL,
    testability_score REAL,
    safety_status TEXT DEFAULT 'pending',
    status TEXT NOT NULL DEFAULT 'active',
    cluster_id TEXT,               -- proximity/dedup cluster, set by evolve
    verification_verdict TEXT,     -- deep verification; see store/db.py
    updated_at REAL NOT NULL,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(id) ON DELETE CASCADE
);

-- Literature/evidence items retrieved for a run; cited by hypotheses via the
-- citations table below.
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT,                     -- 'pubmed' | 'arxiv' | 'mock' | ...
    url TEXT,
    authors_json TEXT,
    year INTEGER,
    abstract TEXT,
    available INTEGER NOT NULL DEFAULT 1,
    mime_type TEXT,
    sha256 TEXT,
    byte_size INTEGER,
    document_version TEXT,
    extraction_tool TEXT,
    doi TEXT,                        -- canonical DOI, when the source has one
    pmid TEXT,                       -- canonical PubMed id, when applicable
    -- What kind of source this is (app/citations/metadata.py's SourceType):
    -- 'peer_reviewed' | 'preprint' | 'database' | 'web' | 'document' |
    -- 'unknown'. Reported, never gated on.
    source_type TEXT,
    passage_text TEXT,                -- exact text (title + abstract) a
                                       -- claim-evidence span's offsets index
    retrieved_at REAL,                 -- when the engine retrieved this
                                        -- article, distinct from created_at
    retrieval_score REAL,               -- hybrid lexical+semantic score
    retrieval_rationale TEXT,           -- the semantic pass's stated reason
    retriever_version TEXT,             -- method/version that produced the
                                         -- score (see relevance.py)
    -- The search that found this evidence (retrieval_calls.id), or NULL for
    -- evidence from another path, such as an upload or directly fetched paper.
    -- Deliberately no retrieval-call foreign key: evidence can be persisted
    -- independently; both tables cascade with their run.
    retrieval_call_id TEXT,
    created_at REAL NOT NULL,
    retracted INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ev_run ON evidence(run_id);

-- Links one hypothesis claim to one supporting evidence row, classified by
-- the four-state citation model in app/citations/ (verified/partial/
-- unsupported/unavailable).
CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    claim TEXT NOT NULL,
    -- verified | partial | unsupported | unavailable
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_id) REFERENCES evidence(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cit_hyp ON citations(hypothesis_id);
-- Cascade deletes from evidence scan the whole table without this.
CREATE INDEX IF NOT EXISTS idx_cit_evidence ON citations(evidence_id);
-- The per-run listing (store.list_citations) filters on run_id and orders by
-- created_at; without this it scanned the whole table -- every run's rows, not
-- just this one's -- and sorted the survivors in a temp b-tree.
CREATE INDEX IF NOT EXISTS idx_cit_run ON citations(run_id, created_at);

-- Reviewer critiques and scores for a hypothesis; one row per reviewing
-- agent pass (reflection, review, meta_review), never updated in place.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    reviewer_agent TEXT NOT NULL,    -- 'reflection' | 'review' | 'meta_review'
    summary TEXT NOT NULL,
    critique TEXT NOT NULL,
    novelty REAL,
    plausibility REAL,
    testability REAL,
    overall REAL,
    created_at REAL NOT NULL,
    author TEXT,
    verdict TEXT,
    detail_json TEXT,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rv_hyp ON reviews(hypothesis_id);
CREATE INDEX IF NOT EXISTS idx_rv_run ON reviews(run_id);

-- Scientist-recorded experimental observations, distinct from reviews and
-- from engine-derived claim/evidence assessments. Each submission is a new
-- immutable row; identity snapshots preserve its context if replay removes
-- the agent hypothesis/evidence rows it originally referenced.
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    winner_id TEXT NOT NULL,
    loser_id TEXT NOT NULL,
    winner_elo_before INTEGER NOT NULL,
    winner_elo_after INTEGER NOT NULL,
    loser_elo_before INTEGER NOT NULL,
    loser_elo_after INTEGER NOT NULL,
    rationale TEXT,
    -- decisiveness class: upset|decisive|clear|narrow
    tier TEXT,
    created_at REAL NOT NULL,
    debate_turns INTEGER NOT NULL DEFAULT 1,
    debate_transcript TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_match_run ON matches(run_id);

-- Safety-gate outcomes at the intake and final-output checkpoints (see
-- app/safety/); one row per gate invocation, kept for audit purposes.
CREATE TABLE IF NOT EXISTS safety_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,             -- 'intake' | 'final'
    decision TEXT NOT NULL,          -- 'allow' | 'redact' | 'block'
    reason TEXT,
    matches_json TEXT,
    created_at REAL NOT NULL,
    category TEXT,
    policy_version TEXT,
    risk_domains_json TEXT,
    requires_review INTEGER NOT NULL DEFAULT 0,
    assessor TEXT,
    resolution TEXT,
    resolved_by TEXT,
    resolved_at REAL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
-- This table had no index at all, so every per-run read of it (the audit
-- listing, the adjudication lookup in safety_stage_is_approved) and every
-- per-run delete scanned it whole.
CREATE INDEX IF NOT EXISTS idx_safety_run
    ON safety_decisions(run_id, created_at);

-- Rendered report snapshots for a run. Multiple rows may accumulate (a
-- report can be regenerated); get_latest_report picks the newest by
-- created_at, so older rows are kept only as history.
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    -- structured report (Overview, Ideas, Tournament, Citations, Safety)
    payload_json TEXT NOT NULL,
    -- full markdown (the single Goal Report document) stored in DB for
    -- durability across restarts.
    markdown_text TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reports_run ON reports(run_id);

-- Chat-style messages for a run: user steering requests and Q&A exchanges.
-- `kind` distinguishes 'steering' (consumed by the workflow, then marked
-- applied) from 'qa' (answered inline, never marked applied).
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    sender     TEXT NOT NULL,
    content    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at REAL NOT NULL,
    applied    INTEGER NOT NULL DEFAULT 0,
    meta_json  TEXT,
    -- Set together, once, when a steering message is acknowledged
    -- (store.mark_steering_applied): when it happened, and the
    -- orchestrator's next_task decision it fed -- "how it changed the
    -- plan" (HITL-STEERING-001), on the message itself. NULL for every
    -- unapplied message and for one applied before this column existed.
    applied_at       REAL,
    applied_decision TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id, id);

-- Durable workflow checkpoints (Milestone 4). One row per saved checkpoint;
-- get_latest_checkpoint reads the newest by seq. `state_json` is the versioned
-- checkpoint envelope (curated workflow state + provider-specific resume data),
-- `last_event_seq` is the high-water mark a resumed run assigns new event seqs
-- above (idempotent replay), and `schema_version` gates fail-closed restore.
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,            -- per-run monotonic checkpoint sequence
    stage TEXT NOT NULL,             -- provider stage/boundary label
    schema_version INTEGER NOT NULL,
    last_event_seq INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ckpt_run_seq ON checkpoints(run_id, seq DESC);

-- Final per-run execution metrics (LLM calls, phase timings). One row per
-- run, upserted at finalize; a resumed run that finalizes again replaces it.
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id TEXT PRIMARY KEY,
    metrics_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- Claim-level entailment graph (Milestone 5). One row per atomic claim of a
-- hypothesis, with its assessed entailment label against retrieved evidence and
-- the exact supporting/contradicting passages that drove the verdict. This is
-- the claim-evidence graph the publication gate reads; it is distinct from the
-- document-level four-state `citations` table.
CREATE TABLE IF NOT EXISTS claim_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    claim TEXT NOT NULL,
    -- supports | contradicts | insufficient
    label TEXT NOT NULL,
    -- categorical | speculative (how the source text presents the claim)
    claim_role TEXT NOT NULL DEFAULT 'categorical',
    supporting_json TEXT,            -- JSON list of supporting passages
    contradicting_json TEXT,         -- JSON list of contradicting passages
    assessor TEXT NOT NULL,          -- provenance id of the entailment assessor
    verification_method TEXT NOT NULL DEFAULT 'legacy_unknown',
    created_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_claim_ev_hyp ON claim_evidence(hypothesis_id);
-- Same reason as idx_cit_run: the publication gate and the report render read
-- this graph per run, and the hypothesis_id index above cannot serve that.
CREATE INDEX IF NOT EXISTS idx_claim_ev_run
    ON claim_evidence(run_id, created_at);

CREATE TABLE IF NOT EXISTS knowledge_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    -- Deliberately NOT a foreign key to evidence(id). A claim-evidence span's
    -- evidence_id is assessor provenance, not guaranteed to be a resolvable
    -- evidence row (e.g. a synthetic passage id from a non-store-backed
    -- assessor input) -- enforcing the FK would abort persisting an
    -- otherwise-valid fact whenever that id does not resolve.
    evidence_id TEXT,
    -- fact | contradiction
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    entities_json TEXT NOT NULL DEFAULT '[]',
    -- the claim_evidence label this row was derived from (supports |
    -- contradicts), kept for traceability back to its source edge
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_kf_run ON knowledge_facts(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_kf_hyp ON knowledge_facts(hypothesis_id);

-- The Supervisor's research plan and terminal state for a run (audit E19).
-- One row per run, upserted at finalize like `run_metrics` -- a resumed run
-- that finalizes again simply replaces it. `plan_json` carries the six
-- guidance blocks the Supervisor's planning call produced wholesale
-- (research_goal_analysis, workflow_plan, config_synthesis,
-- performance_assessment, adjustment_recommendations, output_preparation);
-- `orchestrator_state_json` is the scheduler bookkeeping snapshot as of the
-- last orchestrator decision (previous top Elo, rank-stability counter, pool
-- sizes, last work task). `decision_provenance` and `termination_reason` are
-- the engine's own explanation of its last allocation source and why the
-- run stopped.
CREATE TABLE IF NOT EXISTS supervisor_plan (
    run_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    orchestrator_state_json TEXT NOT NULL DEFAULT '{}',
    decision_provenance TEXT,
    termination_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- One row per scheduled task in the Supervisor's adaptive orchestration
-- ledger (a serialized `scheduling.TaskRecord`, audit E19). Bounded growth:
-- one row per orchestrator decision, and the loop terminates on
-- `max_iterations` (1-4 across the run tiers) or a hard budget ceiling --
-- an order of magnitude below a per-task or per-LLM-call table, which
-- AGENTS.md rules out. Rows are replaced wholesale at finalize (see
-- `replace_supervisor_allocations`), matching the `knowledge_facts`/
-- `matches` pattern, so a re-finalized resumed run does not accumulate
-- duplicates. `seq` is the ledger's own append order -- every row from one
-- drain shares a `created_at`, so ordering by timestamp alone cannot
-- recover the sequence.
CREATE TABLE IF NOT EXISTS supervisor_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    iteration INTEGER NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    -- Observable-facts reason built from live scheduler stats (pool size,
    -- reviewed count, committed matches, iteration) -- never a bare model
    -- claim; see `orchestrator._observable_decision_reason`.
    reason TEXT NOT NULL,
    -- The model's own stated rationale, kept for operator audit only -- not
    -- presented as a factual activity summary (see `orchestrator.py`).
    planner_reason TEXT,
    priority INTEGER,
    termination_reason TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sup_alloc_run
    ON supervisor_allocations(run_id, seq);

-- Durable global scientific task queue. A unique idempotency key prevents a
-- Supervisor retry or worker redelivery from duplicating scientific effects.
CREATE TABLE IF NOT EXISTS scientific_tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    -- queued | leased | paused | completed | failed | cancelled
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    inputs_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner TEXT,
    lease_expires_at REAL,
    result_json TEXT,
    error TEXT,
    -- Bounded history of *failed* attempts only (a success is already
    -- captured by result_json): JSON array, newest last, capped at
    -- store.tasks._MAX_STORED_ATTEMPTS entries. Lets a stalled task be
    -- diagnosed instead of only showing the most recent error, which
    -- used to overwrite every earlier attempt's.
    attempts_json TEXT NOT NULL DEFAULT '[]',
    -- When the *current* lease's attempt was claimed -- set once per
    -- lease, alongside `attempt`, in _try_lease_task. Deliberately not
    -- `updated_at`: a long attempt's heartbeat renews its lease through
    -- renew_task_lease, which bumps updated_at on every renewal, so
    -- reading that column as an attempt's start would report only its
    -- most recent renewal for exactly the slow failures this history
    -- exists to diagnose.
    attempt_started_at REAL,
    -- Not-before instant (epoch seconds) for an otherwise-queued row.
    -- NULL means claimable as soon as queued, which is every pre-existing
    -- row and every ordinary enqueue; a platform rate-limit park (see
    -- store.tasks_lifecycle.park_task_for_rate_limit) is the only writer
    -- that sets it, to the provider's reported cap-reset instant.
    available_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    UNIQUE (run_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_tasks_ready
    ON scientific_tasks(status, priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_tasks_run
    ON scientific_tasks(run_id, created_at ASC);
-- Idle-worker polls and progress counts filter a run's tasks by status.
CREATE INDEX IF NOT EXISTS idx_tasks_run_status
    ON scientific_tasks(run_id, status, task_type);

-- One search: one query, against one source, serving one question. Written
-- by the deep-research capability (co_scientist.platform.retrieval.research) through
-- co_scientist.platform.db.retrieval_calls, after the network work returns -- never across
-- it, since a transaction spanning outbound I/O freezes every other writer
-- for its duration (see the store gotchas in AGENTS.md).
CREATE TABLE IF NOT EXISTS retrieval_calls (
    -- Content id from co_scientist.platform.retrieval.research.artifacts: a hash over
    -- (source, question, query), so re-issuing the same search re-derives
    -- the same id and a resumed run recognizes work it already paid for.
    -- Deliberately NOT unique on its own: the id carries no run, so two
    -- runs asking the same question of the same source share it, and a
    -- bare primary key would let one run's delete cascade take the other
    -- run's provenance with it. The key is (run_id, id).
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    -- The question this search was serving, and its content id. Kept apart
    -- from `query` on purpose: a query is a lossy, source-shaped rendering
    -- of a question, and when it is broadened or retried the question it
    -- was serving has to survive.
    question TEXT NOT NULL,
    question_id TEXT NOT NULL,
    query TEXT NOT NULL,
    source TEXT NOT NULL,            -- 'pubmed' | 'openalex' | 'corpus' | ...
    -- Level of the descent this call was made at, from 1. Without it the
    -- ordering of a replay cannot be reconstructed: a follow-up search and
    -- the first-level search that provoked it are otherwise indis-
    -- tinguishable rows.
    depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,            -- ok | empty | failed
    -- The ranked result set exactly as the source returned it, including
    -- its own ordering and scores. A replay has to reproduce the ranking,
    -- not just the winners.
    hits_json TEXT NOT NULL DEFAULT '[]',
    -- Locators the evidence budget funded, and the ones it refused. These
    -- partition hits_json: "we saw it and did not read it" and "we never
    -- saw it" are different facts about a run, and only the second is a
    -- coverage problem.
    admitted_json TEXT NOT NULL DEFAULT '[]',
    dropped_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,                      -- failure text, when status is failed
    duration_seconds REAL,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, id),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_retrieval_calls_run
    ON retrieval_calls(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_retrieval_calls_question
    ON retrieval_calls(run_id, question_id);

-- Feedback retention and admission history are separate: deleting an old
-- submission must never reset a caller's rate budget.
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    diagnostics TEXT NOT NULL,
    url TEXT NOT NULL,
    run_id TEXT,
    created_at REAL NOT NULL,
    byte_size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at, id);
CREATE INDEX IF NOT EXISTS idx_feedback_owner
    ON feedback(client_id, created_at);
CREATE TABLE IF NOT EXISTS app_llm_usage (
    day INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    calls INTEGER NOT NULL,
    tokens INTEGER NOT NULL,
    PRIMARY KEY (day, client_id)
);
CREATE TABLE IF NOT EXISTS run_announcements (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    prompt_message_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_admissions (
    client_id TEXT NOT NULL,
    host_key TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_admission_owner
    ON feedback_admissions(client_id, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_admission_host
    ON feedback_admissions(host_key, created_at);

-- Persisted application log records captured from the Python root logger
-- (see platform/db/log_capture.py). App-wide: run_id is NULL for records emitted
-- outside any run context. Deliberately no FK to runs -- log history
-- survives run deletion. Retention is enforced by store.prune_capture_logs.
CREATE TABLE IF NOT EXISTS app_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    level TEXT NOT NULL,             -- level name: INFO, WARNING, ...
    levelno INTEGER NOT NULL,        -- numeric level for range filtering
    logger TEXT NOT NULL,            -- dotted logger name
    message TEXT NOT NULL,
    run_id TEXT,
    exc_text TEXT,                   -- formatted traceback, when attached
    client_id TEXT                   -- owning client for ingested UI records
);
CREATE INDEX IF NOT EXISTS idx_app_logs_run ON app_logs(run_id, id);
CREATE INDEX IF NOT EXISTS idx_app_logs_client ON app_logs(client_id, id);
CREATE TABLE IF NOT EXISTS log_ingest_admissions (
    minute INTEGER PRIMARY KEY, requests INTEGER NOT NULL,
    records INTEGER NOT NULL, bytes INTEGER NOT NULL
);

-- Explainable hypothesis-proximity landscape persisted from the engine.
CREATE TABLE IF NOT EXISTS proximity_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_hypothesis_id TEXT NOT NULL,
    target_hypothesis_id TEXT NOT NULL,
    similarity REAL NOT NULL,
    degree TEXT,
    cluster_id TEXT,
    method TEXT,
    version TEXT,
    model TEXT,
    updated_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (source_hypothesis_id)
        REFERENCES hypotheses(id) ON DELETE CASCADE,
    FOREIGN KEY (target_hypothesis_id)
        REFERENCES hypotheses(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_proximity_run ON proximity_edges(run_id);
-- Cascade deletes from hypotheses scan the whole table without these.
CREATE INDEX IF NOT EXISTS idx_proximity_source ON proximity_edges(source_hypothesis_id);
CREATE INDEX IF NOT EXISTS idx_proximity_target ON proximity_edges(target_hypothesis_id);
-- Retired work is explicitly settled without replay; ordinary checkpoint
-- successors remain claimable after a rolling deployment.
BEGIN IMMEDIATE;
CREATE TEMP TABLE _retired_outcome_runs AS
    SELECT DISTINCT run_id FROM scientific_tasks
    WHERE task_type='engine.outcome.refinement'
    AND status IN ('queued','leased','paused');
UPDATE scientific_tasks SET status='completed', result_json='{"retired":true}',
    lease_owner=NULL, lease_expires_at=NULL, error=NULL,
    completed_at=CAST(strftime('%s','now') AS REAL),
    updated_at=CAST(strftime('%s','now') AS REAL)
    WHERE task_type='engine.outcome.refinement'
    AND status IN ('queued','leased','paused');
-- Refinement reactivated a previously completed run. Restore that state only
-- when its published report survives and no ordinary work remains pending.
UPDATE runs SET status='completed',
    updated_at=CAST(strftime('%s','now') AS REAL)
    WHERE id IN (SELECT run_id FROM _retired_outcome_runs)
    AND status IN ('queued','running','synthesizing')
    AND EXISTS (SELECT 1 FROM reports WHERE reports.run_id=runs.id)
    AND NOT EXISTS (SELECT 1 FROM scientific_tasks
        WHERE scientific_tasks.run_id=runs.id
        AND status IN ('queued','leased','paused'));
DROP TABLE _retired_outcome_runs;
COMMIT;
"""

# CREATE TABLE IF NOT EXISTS never alters a table a deployed database already
# has, so each column added after first release is also listed here and added
# idempotently at startup.
ADDED_COLUMNS = (
    ("staged_documents", "peer_hash", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("messages", "peer_hash", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("evidence", "peer_hash", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("run_credentials", "supervisor_provider", "TEXT"),
    ("run_credentials", "encrypted_supervisor_key", "TEXT"),
    ("run_credentials", "custom_models_json", "TEXT NOT NULL DEFAULT '{}'"),
    # NULL means not measured: unsettled, or settled before this column existed.
    ("llm_spend", "refused", "INTEGER"),
    ("anthropic_credit", "refused", "INTEGER"),
)
