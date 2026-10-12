# Operational invariants and incident rationale

Read the relevant entry before changing persistence, startup, workers, provider
bounds, or scientific gates. Each entry states the rule, the code that enforces
it and why. Current model defaults are declared in
`engine/src/co_scientist/core/config.py`.

See [deployment](DEPLOYMENT.md) for hosting and [release readiness](LAUNCH.md)
for release validation.

## Provider escalation bounds

Tool turns use the bounded policy with at most three physical attempts per turn
(`platform/llm/tools/loop.py`). Throttles and temporary outages use jittered
backoff, with a longer schedule for outages (`provider_outage_backoff_seconds`)
because throttle delays can exhaust every attempt before a multi-minute outage
clears. Platform quotas park the durable task; retries are metered in telemetry.
Timeouts, call-budget exhaustion, oversized prompts and failed free admission
stay terminal (`_NEVER_RETRIED` in `platform/llm/attempts/retry.py`). Retries
stop before tool execution and never replay tools from completed turns. The
mandatory-reasoning rung keeps the tool request's raised-budget shape rather
than switching to minimal effort.

## Offline provider isolation

App-side claim assessors bypass the engine router, so they must enforce offline
mode themselves even when a credential is present (`offline_mode()` in
`domains/research_state/claims/grounding.py`, on both the batch and individual
paths). A leak once billed one call per claim group while tests appeared offline,
because a silent provider-error fallback hid it.

## Title validation

JSON-object downgrades do not enforce schema length bounds, so titles are
validated again before persistence (`_TITLE_DISPLAY_CAP` in
`domains/research_state/drain/hypotheses.py`). Mechanistic titles with
gene/receptor names can exceed 100 characters; the 120-character cap
(`MAX_TITLE_CHARS`) accommodates that vocabulary.

## Gotchas

Each of these was a production outage or a silent data-correctness failure. The comments in the code carry the detail; do not re-litigate them from first principles.

- **Never VACUUM from the serving process.** VACUUM and truncating WAL checkpoints need exclusive access, and a SQLite writer waiting for one blocks every writer queued behind it; the log-capture thread writes on every record, so the quiet moment never comes. The symptom is an idle-looking database with every write failing "database is locked". `_reclaim_disk_space()` in `main.py` only prunes superseded checkpoints and `compact_database()` no longer exists (`app/tests/test_diagnostics.py` pins this); the file keeps its high-water mark, and offline compaction means running VACUUM by hand against a stopped database. When a lock symptom appears with an idle database, run `py-spy dump` first.
- **Never hold the SQLite write lock across network I/O, and never write on a poll tick.** There is one writer and no fair queuing, so a transaction spanning an LLM call, or a per-tick write stream, starves every other writer. Hence `assess_hypothesis_claims` (provider work, no DB) is split from `persist_grounding` (writes only) in `domains/research_state/claims/grounding.py`, `_has_claimable_task` probes read-only before `claim_task` opens `BEGIN IMMEDIATE`, and the task heartbeat checks cancellation in memory every second but renews its 300s lease only on the lease's own schedule. The heartbeat is a coroutine on the task's own loop, so synchronous provider waves starve it like a write does: `orchestration/drain.py::_assess_claims` and `orchestration/engine_tasks/gate.py::_assess_gate_claims` run their waves through `core.async_bridge.run_off_loop`.
- **Startup work runs before uvicorn binds a port: keep it cheap and never fatal** (`main.py` lifespan). Run recovery executes interrupted runs rather than scheduling them, so it runs as `asyncio.create_task` with its cohorts on `asyncio.to_thread`; awaiting it put provider calls ahead of the port, failed the healthcheck, and left each restart further behind. The checkpoint sweep must stay inline because it needs the reader-free startup window (a long-lived SSE reader blocks it forever), and it catches `sqlite3.Error` and continues because a full volume cannot journal the DELETE that would relieve it. Serving with a bloated table beats not serving.
- **Only `UnsupportedTaskError` and `LLMCallBudgetExceededError` are permanent task failures** (`orchestration/task_worker/outcomes.py`). Retrying a run that has spent its call ceiling only spends more. Every provider call must therefore pass through `call_llm`/`call_llm_json` so `record_provider_request` sees it: the pre-ranking claim gate once called `litellm` directly and ran far past the ceiling unseen, and now reaches `call_llm_json` through `core.async_bridge.run_coroutine_sync` from the synchronous `Assessor` protocol. `assess_claims_batch` judges a hypothesis's claims in one call, not one per claim, because the gate repeats before every ranking wave, over the finalists and any idea a past check blocked. Every other exception keeps its retry budget; widening the permanent set strands runs (progress in bursts, tasks parked at attempt 1/3 with retries unused).
- **Nothing automatic recovers a `failed` task.** `resume_run_tasks` requeues only `paused`, and the expired-lease rescue skips tasks whose attempts are spent (`_EXPIRED_LEASE_RESCUABLE`). Explicit recovery is the only path back (checkpoint resume, run start, the supervisor's `retry` action via `retry_task`), and any path that re-enqueues a boundary must call `revive_task_for_retry` **before** enqueueing, because the idempotency key cannot change while the run makes no progress and `ON CONFLICT DO NOTHING` would create nothing. Portfolio node tasks key on `{task_type}:after:{predecessor_task_id}` so a lookahead row and the later reactive enqueue collide on one row; bootstrap, resume and continuation rows stay sequence-anchored. Revival takes a `leased` task only once its lease has expired, since reviving a live lease would run the boundary twice. `LLMRateLimitParkError` (a 429 reset too far off for ordinary backoff) is neither a failure nor a retry: `_park_rate_limited_task` returns the row to `queued` with its attempt undone and `available_at` at the provider's reset plus jitter.
- **No process-global asyncio primitives.** Each durable run's worker cohort runs on its own thread and loop (`run_run_worker_pool_sync` calls `run_in_scoped_loop`), so several loops are live per process, and a primitive binds to the first loop that waits on it. The ranking judge semaphore is therefore created per running loop and held weakly (`science/ranking/ranking_debate.py::_get_ranking_semaphore`); as a singleton it killed a ranking task only once waves grew large enough to contend. Bound concurrency per loop or via the cohort's `worker_pool_size`; module scope is safe only for primitives touched solely from the API loop.
- **Never score a short claim against a long document with Jaccard.** Jaccard divides by the union, which the longer side dominates, so the score is capped near `len(claim) / len(document)` however well the document supports the claim. `platform.retrieval.citations.classify_citation` therefore uses coverage (intersection over the claim's tokens), and a new threshold must be sanity-checked against a document that literally contains the claim (`app/tests/test_citations.py`); with the old metric both upper states were unreachable and every citation classified `unsupported`, which read as an evidence-quality problem. Coverage still cannot tell a claim's subject from its assertion; sharpening that is the LLM entailment assessor's job (`domains/research_state/claims/verifier.py`), not a reason to tune deterministic thresholds between hand-picked examples.
- **A model's `contradicts` label is not trusted on its own.** `guard_contradictions` in `claims/verifier.py` keeps it only when a cited quote covers the claim's subject and carries a negation cue (`_quote_negates_claim`, `_CONTRADICTION_MARKERS`) or a separate opposition-verification call confirms same conditions and mutual exclusivity; otherwise the verdict is downgraded to `insufficient`, never promoted to `supports`. Unguarded, topically adjacent or even confirmatory passages were labelled contradictions and withheld most hypotheses from a report. The gate's contradiction-over-support precedence (`claims/gate.py`) is deliberately unchanged: the fix sits where a contradiction is first believed, not where it is weighed.
- **An early gate that never reverses decides the whole run.** A blocking `review_disposition` bars an idea from the Elo tournament (`Hypothesis.is_rankable`), skips comprehensive reflection, and shrinks the evolution pool (`_select_evolution_pool`); a gate that blocked 20 of 22 ideas left a two-idea tournament and an evolution pool that re-derived one drug. So the thresholds mirror the review prompt's rubric (`NOT_VIABLE_SCORE`/`NEEDS_REVISION_SCORE` in `core/constants`) rather than being tuned alone, and only the non-viable band blocks; the batch prompt forces a spread of scores, so a relative low scorer exists on every run. A missing score must be neutral, not the worst score (json_object mode does not enforce the schema). The gate is not terminal: `review_gate.derive_review_disposition` re-derives from every review the hypothesis holds (the mature cascade wins except over `unsafe`), `refresh_review_dispositions` re-runs it at zero LLM cost, and each blocked idea gets exactly one recurrent review per run (`review_recheck_issued`, checkpointed so a resume cannot re-fire it, capped at `MAX_RECHECKS_PER_RUN = 24`). Re-reviewing every blocked idea every cycle was rejected because blocked ideas are the population that grows when the gate misfires. "Excluded from the report" is also not one fact: `duplicate` and `rejected` share `EXCLUDED_HYPOTHESIS_STATUSES` but must reach the reader as different words.
- **The near-duplicate guard must see everything it guards against.** `evolve.py` passes `other_hypotheses_texts` to `_apply_evolution_result`, which discards too-similar children. They are sampled from the whole pool (`sample_context_hypotheses`: top 5 by Elo plus random, capped at 15, which bounds token cost); sampling from the round's `top_k` left the guard blind to every idea outside the round and proximity archived the duplicates only afterwards.
- **Counts named for different things are computed differently.** The report payload carries `idea_count` (everything explored), `hypothesis_count` (released by the safety/contradiction gates) and `verified_count` (released and carrying a `supports`/`partial` claim edge). `domains.report.gates._verified_hypothesis_count` is the exact complement of `unverified_hypothesis_ids` over the released set, because the tile and the per-idea badge are one fact shown twice. `idea_buckets` must partition: `non_viable` is everything not in `high_potential`, so capping either half stops the pair summing to the idea count.
- **Structured-output schemas must not echo input back.** Identify pool items by the positional index the prompt assigns, never by repeating their text: an echoing schema makes output length scale with the pool, overruns the token budget, truncates the JSON identically on every retry, and degrades silently (this is how proximity clustering stopped deduplicating; `_match_cluster_member` in `science/proximity/proximity_dedup.py` keeps text-prefix matching only as a fallback). Trim the schema, never the input: proximity sends the whole pool in one prompt and must keep full hypothesis text (`_prepare_hypotheses_for_analysis`), because it is asked to find differences that live in a hypothesis's tail and its verdict deletes work. If the prompt is too large, chunk the pool.
- **`max_tokens` funds the chain of thought too.** Reasoning counts against the same allowance, so an answer-sized budget lets thinking consume all of it: `finish_reason="length"`, empty `content`, paid in full. Every request takes its floor from one place instead of trusting per-site budgets: `_apply_thinking_args` adds `THINKING_FLOOR_MAX_TOKENS` for engine calls and for `llm_request.acompletion`, whose callers send only the answer budget and whether to think. Omitting `max_tokens` is not safe either: the provider default is small enough for thinking to exhaust, and an unbudgeted safety screen once held every run for human review on a truncation. The standing rule: **an app-side call that parses JSON from a one-shot completion goes through `call_llm_json`, not a direct `litellm.acompletion`** (a bare `json.loads` also failed on a model that fenced its JSON in Markdown, holding every run at intake). App streaming and plain-text calls (`domains/chat/interviews/model.py`, `qa/__init__.py`, `run_start_announcement.py`, `goal_text.py`, credential probes) go through `platform.llm.llm_request.acompletion` and `complete_request`; they keep streaming and caller deadlines but have their own operation budget (`APP_LLM_MAX_CALLS`, default 4) instead of consuming a run's allowance, count one physical attempt only after free admission, and summarize usage once per operation. Streaming requests ask for final usage and keep missing usage unknown.
- **A floor is not a guarantee, so the retry has to change the request.** Retrying a budget failure unchanged is the same doomed call billed again. `_extract_completion_content` (`platform/llm/request/response.py`) splits an empty response by `finish_reason`: `"length"` raises `LLMBudgetExhaustedError` (a `ValueError` subclass); a normal stop that spent reasoning tokens and wrote no answer raises `LLMThinkingOnlyError`, which skips straight to the top rung because the model chose to stop and never wanted for room; an empty response with no reasoning stays a plain `ValueError` and a plain retry. `BudgetEscalation` is the ladder, climbed only by `run_attempts` so `call_llm`, `call_llm_json` and the tool turn cannot drift apart: raise the budget by half again (floored at `BUDGET_ESCALATION_MAX_TOKENS`, increment capped by `BUDGET_ESCALATION_MAX_INCREMENT`; a flat max silently no-ops for callers already at that constant), then thinking off. The last rung makes it terminate, since no finite budget is provably enough. A schema or parse failure keeps the current rung because more tokens do not fix a wrong answer; only `call_llm_json` can raise one, which is why it defaults to 5 attempts and `call_llm` to 3 (exactly the three rungs). The failure log reports the effective budget (`effective_max_tokens`), not the pre-floor number, so it does not read as a provider fault.
- **Classification calls and models that cannot disable reasoning.** Entailment is classification, not reasoning work, so those calls request thinking off from the first attempt (`LLMCallOptions(enable_thinking=False)`, `max_attempts=3`). The wire shape is decided by `platform/llm/request/thinking.py`, not the call site: a model whose `ModelProfile.reasoning_can_disable` is false (the default for every declared free-chain entry) is sent a capped reasoning knob (`MINIMAL_REASONING_MAX_TOKENS`) instead of a bare disable, funded by the same floor (`effective_thinking_enabled`), so the redirect cannot reproduce an answerless completion. For a model the table gets wrong, `escalation_for_error` recognizes the "reasoning is mandatory ... cannot be disabled" 400 (or a rejected reasoning cap) and escalates once to `MINIMAL_REASONING_REQUIRED`, forcing reasoning on at low effort via `scoped_minimal_reasoning`; one recovery attempt only, so a provider that rejects that too still exhausts its budget. Without this, every hypothesis fell back to the deterministic lexical assessor.
- **The token budget and the wall clock are one setting in two places.** Funding a chain of thought without extending the deadline only moves the failure to a mid-reasoning abandonment, and both land in the same silent fallback. `thinking_safe_timeout` raises the deadline like `_apply_thinking_args` raises the budget (`THINKING_FLOOR_TIMEOUT_SECONDS` derives from the token floor). **On a streaming call, bound silence, not duration** (`platform/llm/llm_scope.py::stream_chunks`): a stream delivering tokens is healthy however long it runs, and on a thinking model long-but-alive is normal. Interview and Q&A streams relay reasoning as it arrives, so a generous deadline there is visible progress; do not copy those numbers to a blocking call that shows nothing. A stream can also end clean having spent every reasoning token and written no answer, which is not a provider failure: the interview path retries once with thinking off (`enable_thinking=False`), as do `domains/chat/goal_text.py` and `run_start_announcement.py`, without importing the engine's ladder.
- **A per-item LLM pass belongs to the caller that runs once, not per hypothesis.** The model-judged relevance pass (`apply_semantic_relevance`) costs one call per candidate; the literature-review node spends it once per run, but deep verification, comprehensive reflection (`full`) and evolution grounding all reach search through `evidence_context._retrieve_probe_evidence`, per hypothesis per cycle, multiplying cost by pool size and iteration with no error. `SearchConfig.semantic_relevance_enabled` is the opt-out, set to false at that one shared seam. Before adding a per-item LLM pass to a shared helper, check every caller's multiplicity; when a phase's cost jumps, read per-item telemetry (`scientific_tasks.result_json` carries each fan-out item's `model_usage`) before reading code.
- **A tool's `parameter_mapping` is written in its caller's vocabulary, and an unmapped name is passed through, not dropped** (`ToolConfig.map_parameters`). Two vocabularies reach it: the search paths send `query/slug/max_papers/recency_years/run_id` and context enrichment sends `entity_name/limit`. A mapping written for the wrong one makes the MCP server reject the whole call on validation (Europe PMC and preprint search returned nothing to any run until fixed). A tool that takes an id rather than a query must not be `category: "search"`, since novelty validation calls the first search-category tool of its workflow; workflow tool lists contain only tools whose input that caller can send. No test binds configured mappings to the real `engine/mcp_server` signatures, so check both sides when either changes.
- **Under the json_object downgrade, reshape the answer to the schema in both directions.** A model without server-side schema enforcement omits required fields and invents extra ones, and every object node from `core/json_schema.obj` is closed (`additionalProperties: False`), so one invented key fails the whole response; feeding the validation error back did not help across five paid attempts. `reshape_json_output` (`platform/llm/structured/validate.py`) removes undeclared keys, fills required fields and cuts over-long `maxItems` arrays and `maxLength` strings (at a word boundary where possible). It runs only when `_supports_json_schema_response_format` is false (`json_attempt._backfill_and_validate`), so where the provider enforces the schema an extra field stays a real validation failure.
- **Two independent wall-clock ceilings on outbound calls.** `COSCIENTIST_LLM_TIMEOUT_SECONDS` (default 600s) bounds `litellm.acompletion` only; MCP tool calls are bounded by `COSCIENTIST_MCP_TOOL_TIMEOUT_SECONDS` (default 300s). Setting either to `0` disables it. The two MCP call sites diverge deliberately: `call_tool` raises `MCPToolTimeoutError` (callers degrade per source), while `execute_tool_call` returns the timeout as the tool's result, because it runs under an `asyncio.gather` without `return_exceptions` where raising would kill every sibling call.

## Durable state reducers

Derive commit reducers from every accumulating `WorkflowState` channel
(`channel_reducers` in `orchestration/task_runtime.py` reads the `Annotated`
hints). A hand-maintained table once omitted `tournament_matchups`, silently
replacing prior cycles of Elo history instead of appending them.

## Durable node names

Graph node keys persist in task types, checkpoints and idempotency keys.
Renaming a node requires a migration even when its implementation is unchanged.

## Contextual safety review

Danger nouns alone do not establish operational intent. Deterministic context
matches cannot clear themselves: only a contextual assessor can clear a held
verdict or strengthen it to a block. Operational hard blocks bypass that assessor;
unavailable or ambiguous review remains held.

Redact both report payload and Markdown: reports and events
expose them independently. Final-report redaction leaves the separately served
claim-evidence facts unchanged.

## Advisory reachability

Online audits (`make audit-deps`) retain every finding from all five locks (three
Python, two Bun). Findings in LiteLLM proxy routes, FastMCP Windows/OAuth paths
and pickle-backed DiskCache were outside the deployed execution paths, not
permanent waivers. Reassess reachability when exposing those paths or changing
the entry point, host OS or storage topology; upgrade and validate deliberately
rather than suppressing them.

## Credential boundaries

Run ownership is a caller-selected per-browser client ID, not verified
identity; it keeps one browser's work apart from another's but does not
authenticate a person. Keep MCP private with matching shared secrets.
BYOK encryption uses a separate key: rotation requires migrating or removing the
affected stored credentials. Keep database sidecars, caches and outputs out of
commits and image build contexts.

Use one provider-credential map for offline selection and semantic safety:
separate maps mistook credentialed Azure/Google deployments for keyless ones
and silently skipped provider work or contextual review.

Resolve every staged-document ID against its owner before committing a run or
interview; partial attachment silently changes the requested evidence. Deleting
a staging record leaves text already copied into a run as that run’s evidence.

## Known limitations

- **Ownership is not authority.** A client ID must never confer safety-review
  power: adjudication requires the operator token (`LOGS_ADMIN_TOKEN`, sent as
  `X-Logs-Token`; `has_admin_token` in `api/operator_access.py`), checked in
  `adjudicate_safety` and honoured by the ownership middleware for any run.
- **Engine calls spend the shared quota.** Every deployment-funded provider call,
  including engine calls, reserves global durable quota (see Provider admission).

## Claim adjudication

A contradiction requires a verbatim, on-topic negating quote. Off-target or
confirmatory passages cannot block a hypothesis, including deterministic fallback
under LLM provenance. Tolerating passage numbers or legacy evidence IDs never
bypasses the requirement that the quote occur in evidence actually shown.
Ground passage-sized chunks before variable claims to preserve cache reuse.

## Classification and held tasks

Classification calls disable reasoning or use the smallest capped mandatory
reasoning budget; raising output floors can fund more reasoning rather than an
answer. A configured but unreachable safety assessor refuses; offline mode must
be deliberate. Safety holds park claimable tasks without spending retry attempts,
and re-enqueuing an already-succeeded idempotency key cannot revive the task.

## Cost accounting

Cost accounting uses measured provider cache-read rates. The Azure ledger uses
integer micro-EUR and snapshots prices and FX for each reservation. Paid
admission commits durably before dispatch; no database writer spans provider
I/O. The native client rechecks kill switches, expiry and spend holds after a
thread wait, immediately before sending. Settlement failure retains the
reservation and blocks further paid calls.

See [operator credit setup](azure-setup.md#spending-cutoff) for allowances,
restore holds and settlement, and [prompt caching](llm-caching.md) for provider
cache pricing. The ledger covers model tokens; other subscription charges also
reduce the provider credit.

## Queued work and log cursors

Future-due queued tasks keep their worker cohort alive until they become claimable.
Retention and scoped log clears leave ID gaps: count matching rows after a cursor,
rather than subtracting cursor IDs.

## Retention

Staged documents default to 30 days (`COSCIENTIST_DOCUMENT_RETENTION_DAYS`),
terminal runs to 90 (`COSCIENTIST_RUN_RETENTION_DAYS`) and drafts to 7
(`COSCIENTIST_DRAFT_RETENTION_DAYS`); `0` disables that expiry
(`domains/access/retention.py`). The hosted deployment sets run retention to `0`.
The API schedules `sweep_all` hourly after startup, off the port-binding path,
with short per-run transactions. It also prunes seven-day admission history
in bounded batches. The module remains available
for operator maintenance (`python -m co_scientist.domains.access.retention`).
A separate Railway cron cannot reach the API volume. Never hold a writer over
network I/O, VACUUM or truncate the WAL from the serving process.

Staged document expiry does not remove text already copied into chats/runs;
those copies follow their parent's lifetime. See [data and privacy](#data-and-privacy)
for the complete inventory and external backup decisions.

## Gateway routing and privacy

OpenRouter accepts at most three entries in a models array; free allowances belong
to individual models. Preserve upstream preference for cache locality rather than
round-robin hosts. `require_parameters` binds support to the selected host, and
a profile's declared zero-retention restriction binds its checked upstream
host. Unpinned free/trial profiles do not establish that restriction; do not
promise that every route avoids logging or training.

## Ranking and steering

Repeatedly judging the sole pair in a two-idea pool adds no evidence and inflates
Elo. Steering is consumed on observation, so it buys one cycle beyond an exhausted
ceiling to incorporate the input before marking it applied.

Global launch pause rejects new steering, attachments and chat changes before
their writes commit. Guidance and a completed run's continuation admission share
one transaction. Replies already admitted may finish during an ordinary pause;
draining still interrupts them. Guidance admitted during final publication stays
pending if pause prevents reopening, and operator resume retries it under the
same provider, capacity and daily allowance checks.

## Durable fan-out recovery

Propagate budget and rate-park exception types through gathered work so the
worker can terminate or park it correctly. Permanent failure or plan divergence
cancels dependent portfolio tasks in one transaction. An unknown provider outcome
permanently fails a fanout item without replaying it or cancelling siblings;
aggregates admit failed items and preserve an explicit unreviewed verdict.
Provider timeouts on coordinator tasks still stop the run. An expired coordinator
lease instead pauses the run and queued work, preserving checkpoints and attempt
history. Owner-confirmed `POST /api/runs/{id}/resume` acknowledges possible duplicate
requests and route-dependent charges before readmitting the saved boundary; this also recovers
historical unknown-outcome failures with a checkpoint. Pending safety decisions,
live leases and launch/capacity/provider admission still block recovery.
Provider credit rejection pauses its task and queued work without cancelling
fanout dependents. Top up the provider account, then explicitly resume the saved
boundary. Known admission rejection needs no unknown-outcome acknowledgement;
replaying the unfinished stage may still repeat earlier calls within that stage.
The run's own LLM-call ceiling remains terminal and cannot be reset by resuming.
Lost leases may retry automatically only under persisted zero-price admission,
without caller keys: the
`zero_cost_admission` stamp a free run on all-free routes gets at creation,
which binds its engine tasks to zero-price-only requests. Unstamped coordinator
work waits for explicit replay approval. Zero-price coordinator work also pauses
when its automatic retry budget is exhausted.
A restart does not wait out the 300 s lease: graceful shutdown releases the
process's own leases, and startup recovery expires every live lease on an
unpaused run whose owner lacks the new process tag (`expire_earlier_process_leases`,
sound only at one api replica), so the rules above decide it within seconds.

Verification issuance markers survive failed attempts and checkpoint restore;
otherwise recovery funds the same evidence pass again. Periodic companion nodes
(meta-review and research overview) run serially and retain their list on
restore to prevent checkpoint forks (`stack_companions`: parallel successors fork
the checkpoint chain). Likewise, running proximity beside the research overview
would give one run two concurrent checkpoint writers, which the single-writer
checkpoint invariant forbids. Successful simulation is not reissued because
full review failed, and a failed simulation is not retried after full review
succeeds.

## Usage and failure attribution

Price the served model while retaining its gateway prefix: unprefixed names can
turn paid calls into apparent zero-cost usage. Lower layers log failures at
debug; the retry boundary emits one reader-facing record per attempt with the
actual sent budget and call identity.

## Tool transcript recovery

Age tool results before deduplication: an aged note must not erase a newly
retrieved body. Keep positional call/result pairing and prior reasoning while
eliding superseded writes; later patches do not supersede earlier relative edits.
Interrupted commands may already have side effects, so aborted output must not
claim the command never ran.

Workspace spill writes run outside confinement and must resist symlinked or
missing metadata paths. Redact stdout before model transcripts and drop the
output if a secret still survives.

## Settlement and structured answers

Use the same match-debt quantity to size, open and close a settlement episode.
Do not refill while debt persists; bound the episode by distinct pairs.
Place the answer-deliverable instruction before an injected response schema;
placing the same instruction afterward did not prevent empty thinking replies.

## Search widening and scroll timing

Keep PubMed automatic term mapping on exact/recency rungs; broader field-tagged
queries retain leading subject anchors. Fully ORing a starved query can return
huge unrelated corpora. Strip wildcard syntax on ordinary OpenAlex search
(API 400s), and distinguish source refusal from successful empty retrieval.

Programmatic scroll anchors do not establish user follow intent. ResizeObserver
callbacks must defer writes to observed text to avoid loop errors; batch geometry
reads and writes to avoid repeated layout flushes. Theme suppression lasts two
animation frames.

DOM growth is not a reader gesture: compare scrollTop with the last applied
bottom scroll before measuring the new gap; asynchronous scroll events can lag
the gesture. Rearm initial landing by conversation identity because chat switches
reuse the workspace rather than remounting it.

## Retrieval scores and scheduling ceilings

Capture lexical retrieval scores before stamping hybrid scores. Reusing the
overwritten score double-weights the semantic term and can collapse rankings.

Every MCP search returns `{"status": "ok", "records": [...]}` or
`{"status": "failed", "records": [], "error": "<short reason>"}` from
`engine/mcp_server/tools/_results.py`, without raising. The engine reads
`status` and treats a reported failure as a permanent query failure, and its MCP
client raises `ToolException` for any execution error rather than accepting
FastMCP's error text as a result. Transient transport failures use bounded,
jittered retries (`call_search_tool`); distinguish failed queries from
successful zero-hit responses. Failed novelty retrieval stays unknown, receives
no novelty credit, and its sources are named in stored reviews and reports.

Owed review cannot override the provider-call ceiling: scheduler and transport
use the same counter, so its first call would turn budget termination into a
permanent task failure. Spend its bounded per-hypothesis marker at issuance,
regardless of success; settlement coverage retains its separate scheduling policy.

## Run capacity, interview completion and safety decisions

Count concurrent runs across all tiers; per-tier counting multiplies the
advertised allowance. Provider spend belongs to each tier's call budget.

Model-confirmed completion requires challenge/focus and accepts explicit no
constraints; fallback completion must collect a preferences answer first.

Hypothesis-stage adjudication concerns an already-excluded idea and leaves the
whole-run lifecycle untouched. Intake/final decisions govern the run's hold.

## Checkpoint successors and terminal synthesis

Persist each checkpoint's exact resume successor; recovery rebuilds the same
predecessor-based idempotency key. Bootstrap must reach supervisor guidance before
orchestration; defaulting to the orchestrator can resume an unplanned run.

Only exhausted durable retries may degrade optional terminal synthesis to an
empty section, allowing finalization to publish completed work. Budget, parking
and cancellation control-flow errors still propagate to the worker.

Batch tournament progress to expose liveness without flooding the bounded
event feed with per-match events.

Bound both per-source content and total corpus size: neither cap substitutes
for the other. The 130-source cap (`RESEARCH_OVERVIEW_MAX_SOURCES`) targets about
52,000 evidence tokens; adding full texts requires selecting passages rather than
lifting those caps.

Fit synthesis to the provider clock as well as its output allowance: at tens of
tokens per second a 42,000-token request cannot fit the 600-second default
deadline. Outline once and write bounded parts, preserving successful siblings
and grounded draft fallbacks when a part fails.

## Safety deferral and proximity storage

Unsafe-content stops never defer to owed review or tournament coverage.
Cleanup may exceed ordinary work ceilings only within its settlement allowance
or permanent issuance markers; provider admission still caps new requests.

The proximity edge floor (`PROXIMITY_EDGE_FLOOR`) bounds checkpoint size and the
SQLite writer's insert workload, while every pair is still measured. Round before
admission so the persisted similarity is the value compared against the floor.

Size idea/match ceilings above each tier's productive steady state. Both match
participants count toward coverage, and limits checked before work can otherwise
terminate healthy runs after their first tournament.

Public planning overrides retain tier baselines and reject excess values before
persistence. Initial hypotheses and evolution are capped at the tier's idea
ceiling (Express 12, Standard 32, Extended 60, Ultra 96); iteration maxima are
2, 4, 6 and 8 respectively. Elo K is positive and at most 512. Restored plans,
direct generators and generation fan-out also enforce allocation ceilings.

Tool turns admit at most 16 calls with four concurrent readers, preserving
write/process barriers and paired errors for declined calls. MCP admits eight
tools across the process; URL bodies remain at most 10 MiB and only two PDF
workers may run. PubMed metadata searches admit 1–400 papers, fulltext searches
1–20, with 4 MiB per Entrez response, 16 MiB across a tool call and 1 MiB of
serialized output. Capacity refusals release admission on failure/cancellation;
blocking parsers and Entrez workers retain it until they finish.
DNS has eight separate process-wide worker slots, held by the actual threads
after request deadlines or cancellation so abandoned lookups cannot accumulate.

Draft workspaces close on every exit. Persistent commands expire after five
minutes; each workspace admits four and the process admits sixteen. Linux
execution requires Landlock ABI 3 (Linux 6.2+) for path truncation confinement.
No-network seccomp also refuses io_uring setup, enter and register operations.

Empirical outcomes and their targeted refinement are retired. Schema initialization
removes `outcome_refinement_actions` and `hypothesis_outcomes`, then settles pending
`engine.outcome.refinement` rows as completed with `{"retired": true}`. The retired
dispatch path also returns that result without model calls. Scientific checkpoints
and ordinary task dependencies remain intact for recovery; no retired call replays.
A run reactivated solely for refinement returns to completed when its published
report survives and no ordinary work is pending.

## Feedback and operational diagnostics

Application and provider diagnostics export source locations, numeric metadata,
run/trace identifiers and fixed exception classes to stdout and `app_logs`.
Free-form research and raw model-response/exception text are withheld centrally.
Browser-submitted diagnostics retain their existing ownership and consent rules.
For deployments that previously captured private research, the deployment owner
must review retained `app_logs`, infrastructure logs and backups for erasure or
expiry; source changes do not remove historical copies.

`POST /api/feedback` requires the caller's `X-Client-ID` owner identity and
accepts one of five fixed categories, a message (8,000 characters), the session
diagnostic export (100,000), a URL (2,048) and an optional run ID (128). The run
ID is context only: it never grants access to that run or its artifacts.

Maintainers read the `feedback` table in the SQLite store; there is no HTTP read
route and no SMTP delivery. The store keeps the newest 200 submissions within a
10 MiB budget and prunes records older than 30 days. Admission allows 5 per
owner, 20 per connecting host (hashed, never returned) and 100 globally per
rolling minute, retained across restarts and row eviction; excess requests get
HTTP 429 with `Retry-After` set to the seconds until the blocking window
resets (1 to 60).

The attached export keeps the newest loaded records within the limit; a failed
log fetch becomes an explicit diagnostic-unavailable record so the message can
still be sent. Failed fetches are recorded as method, path and status without
query strings or payloads.

## Run efficiency

A run spends its depth on its finalists. Every idea gets the safety screen and
one screening review, four ideas per call (`SCREENING_BATCH_SIZE` in
`orchestration/engine_tasks/fanout.py`). Only the tier's finalists (`finalists`
in `RUN_TIER_DEFAULTS`: 3, 5, 6, 8) get depth: one finalist review that
answers the observation, full and simulation parts in a single call
(`review_finalist` in `science/reflection/comprehensive_reflection.py`), then
one deep-verification call that first searches the review's own queries.
Express analyzes its literature corpus in one call. Within a run, a claim check
of an unchanged claim against unchanged sources reuses the stored verdict, even
across the gate and finalize, which name the same passage differently, and
the tournament skips a pair already judged with unchanged inputs; both record
the reuse in provenance. Before these changes an Express run could spend
over 100 calls, mostly on depth and claim checks for ideas the report never
featured.

The offline meter (`app/tests/test_run_envelopes.py`) drives every tier through
the durable worker against a fake MCP server and fails when a tier passes its
ceiling or a call goes out without its call type, which would give it the
generic effort. Lower a ceiling when a change lowers a tier; never raise one to
make a regression pass. `METER_OUT` writes each tier's breakdown by phase and
call type. Offline counts on 8 October 2026, before and after the change (the
median of three runs; counts move a few percent with fan-out order):

| Tier | Calls before | Calls after | Requested tokens before | Requested tokens after |
| --- | --- | --- | --- | --- |
| Express | 118 | 50 | 1.13M | 0.63M |
| Standard | 301 | 124 | 2.91M | 1.55M |
| Extended | 528 | 219 | 5.08M | 2.59M |
| Ultra | 688 | 312 | 6.55M | 3.56M |

The offline judge is position-consistent, so every debate stops after one turn
and the tournament's own saving shows only on live runs. Live runs also add
retries, budget escalation and fallbacks to these counts.

`max_llm_calls` in `RUN_TIER_DEFAULTS` is a runaway cap, 20 to 45 times the
measured use, not an envelope. It also gates features: knowledge-base synthesis
needs 2,500 and interim research overviews 7,000, so lowering it changes what a
tier produces, not only what it costs.

## Provider admission

Provider admission is separate from research-run budgets. Every operator-funded
physical completion, engine or app, atomically reserves one call and a token
allowance in SQLite before dispatch (`platform/db/admission.py::reserve_provider`).
The allowance is conservative: prompt bytes plus 1,024 plus the request's whole
output cap (`_token_reservation` in `platform/llm/admission/service.py`).
A successful call with reported usage then settles to its real prompt plus
completion tokens (`settle_provider`, refunding the difference in one
transaction, at most once per receipt). Failed calls, missing usage and
interrupted streams keep the full reservation because billing may be
uncertain. UTC-day quotas survive restarts and run/chat deletion.

Scopes and defaults: global `APP_LLM_GLOBAL_CALLS_PER_DAY` /
`APP_LLM_GLOBAL_TOKENS_PER_DAY` (1,024 calls, 32M tokens; also covers rotated
client IDs and every funded engine call), per client
`PROVIDER_CLIENT_CALLS_PER_DAY` / `PROVIDER_CLIENT_TOKENS_PER_DAY` (512, 16M) and
per host `PROVIDER_HOST_*` (512, 16M), plus per-client app-chat
`APP_LLM_CLIENT_CALLS_PER_DAY` / `APP_LLM_CLIENT_TOKENS_PER_DAY` (64, 2M). All
must be positive. `APP_LLM_MAX_OUTPUT_TOKENS` (32,768) and
`APP_LLM_MAX_INPUT_BYTES` (256,000) bound one app request; engine requests use
`PROVIDER_MAX_OUTPUT_TOKENS` (131,072) and `PROVIDER_MAX_INPUT_BYTES`
(1,000,000). These are usage limits, not a price quote; model selection sets the
cost per token. BYOK calls skip the reservation, keep the operation cap and use
the caller's funding. Session-start announcements are claimed once per run;
repeats replay the stored reply, or a deterministic confirmation if the original
stream was interrupted.

**Reservation ceiling.** Without settlement, reserving the whole output cap
per call ran about four times a run's real use, so a Standard run exhausted the
default 16M `PROVIDER_CLIENT_TOKENS_PER_DAY`. Keep settlement rather than
raising the ceilings or shrinking the reservation: the reservation must cover
a call whose usage never arrives.

## Load and capacity

Reproduce the isolated workload with [evaluations/load](../evaluations/load/README.md).
The API uses the production image, one process, `WORKER_POOL_SIZE=1`, SQLite WAL
on a disposable volume, 2 CPU / 2 GiB limits and deterministic 100 ms completion
I/O. Runtime networking is internal-only. These measurements describe API/store
capacity, not real model/MCP throughput, production ingress or paid quota.

The five-minute mixed workload offers 500 new landing visitors/minute, 200 owned
run pages, 50 starts, 20 report visits/minute and 10 streamed Q&A turns/minute,
including cross-origin preflights. The measured envelope is **450 landing
visitors/minute plus 200 concurrent run pages** with the peer qualification
below. The fixed workload achieved 468.4 visitors/minute, 100 report visits and
50 Q&A turns; all 44,028 requests had zero unexpected errors. Fifty starts
admitted six and clearly refused 44. Do not quote the offered 500 as achieved.

| Measurement | Before repair | After repair | Over-cap probe |
|---|---:|---:|---:|
| Duration / offered visitors per minute | 300 s / 500 | 300 s / 500 | 120 s / 1,000 |
| Achieved landing visitors per minute | 482.2 | 468.4 | 749.5 |
| Attempted / peak live run pages | 200 / 8 | 200 / 200 | 300 / 256 |
| Unexpected errors / requests | 8 / 30,373 | 0 / 44,028 | 0 / 28,191 |
| Unexpected error rate | 0.026% | 0% | 0% |
| Peak API RSS / sampled FDs | 374.0 MiB / 456 | 382.5 MiB / 482 | 350.5 MiB / 658 |
| Demo GET p50 / p95 / p99 (ms) | 96 / 300 / 630 | 110 / 420 / 1,200 | 93 / 490 / 740 |
| SSE headers p50 / p95 / p99 (ms) | 36 / 2,200 / 2,400 | 160 / 2,100 / 3,600 | 510 / 2,600 / 2,700 |
| Report GET p50 / p95 / p99 (ms) | 25 / 130 / 220 | 23 / 230 / 740 | 32 / 540 / 800 |
| Chat p50 / p95 / p99 (ms) | 160 / 1,800 / 2,800 | 250 / 1,200 / 1,300 | 320 / 2,500 / 2,500 |
| SQLite statement p50 / p95 / p99 (ms) | .17 / 3.32 / 6.07 | .23 / 4.49 / 10.06 | .33 / 5.33 / 11.94 |
| Writer acquisition p50 / p95 / p99 (ms) | .36 / 53.98 / 137.42 | .50 / 54.90 / 229.62 | .02 / 1.99 / 2.83 |
| Loop lag p50 / p95 / p99 (ms) | 0 / 7 / 125 | 0 / 18 / 232 | 0 / 14 / 270 |

The historical before run used forty generator containers without OPTIONS;
after/over-cap use one generator with forty actual source-address aliases and
OPTIONS. The final after image also includes intervening main changes and the
model double resolves schema unions; before allowed only eight streams, so it
did less stream work. These are capacity receipts, not a controlled assertion
of latency improvement.
A separate otherwise identical sixty-second HTTP keepalive comparison reduced
history connection resets from 17/5,483 requests (five seconds) to 0/5,522
(fifteen seconds), with 200 streams in both trials. An integrated repeat still
had 12 history GET idle-socket resets/44,501 requests (0.027%), without a raw 500. A forced-close
wire probe shows Chromium transparently repeats that safe GET while Requests
fails. The load client now permits one safe read retry before headers, never
replaying POSTs, statuses, connect failures or streaming bodies; its regression
checks GET recovery and no POST replay. The final repeat had zero unrecovered
errors and zero recovered retries, so it does not prove that retry caused the
difference. Both repeats are retained in the receipt. No SQLite busy/locked
exception occurred in these trials. Timing windows are bounded, SQL writer
acquisition includes syscall time, and one-second FD samples can miss short
peaks. The fake backend does not establish real-provider memory/throughput or
long-duration leak behavior. Exact image IDs, source overlays and route numbers
remain in local load outputs and are not distributed with the source.

The first broken limit was GET SSE sharing the unset write-only peer context:
all viewers hit one eight-stream bucket. Request-peer admission fixes that,
with configurable global capacity and clear 429/Retry-After messages. Quiet
streams now poll at 0.5, 1, then 2 seconds, returning to 0.5 after events; status
fallback and keepalives keep their wall-clock deadlines. No writer or network
I/O is added to polling. Above the measured envelope the 256-stream guard shed
176 excess attempts without unexpected errors; the 1,000-visitor target was
not sustained. Raising caps is not established capacity.

SSE capacity is bounded globally by `SSE_MAX_CONNECTIONS` (default 256), by browser
identity (four), and by connecting host (eight; IPv6 shares a /64). Client-supplied
forwarded headers do not establish identity. Missing peers share one bounded
bucket. A common ingress/NAT peer therefore permits only eight streams; the
200-page result requires enough distinct connecting peers and does not prove
that production ingress preserves them. Verify ingress identity before claiming
the local capacity for production. Refusal returns 429 with a human-readable
message and `Retry-After: 30`; the run view explains the refusal and waits at
least thirty seconds before reconnecting.

Run starts are a separate budget. The default global free limit is twenty/day,
six/day per host and three/day per browser. This is a ceiling of 0.83 free
starts/hour averaged over a full day, not a model completion-rate promise.
Admitted runs may still exhaust their physical-call/token allowance. The measured
50-attempt burst admitted six and refused 44 with a daily-admission message;
the final integrated repeat completed three and stopped three at the shared
host's 512-call allowance, with the stored daily-provider-admission message. Do
not interpret six accepted requests as six completed runs. One complete Express
probe and report Q&A were separately verified using the same deterministic
backend. Check the run efficiency and model envelopes before
allocating all twenty daily starts. A separate sixty-second profile with
`MAX_CONCURRENT_RUNS=1` admitted two of ten starts, clearly refused four at the
concurrent-run guard and four at the daily host guard; both admitted runs
completed and no unexpected request error occurred.

### If traffic spikes

1. In Honeycomb, group `HTTP POST` spans by `http.response.status_code` and
   compare counts and p95/p99 duration (`http.request.method=POST`). Watch 429
   intake refusals, unexpected 5xx, and `task.execute` spans grouped by
   `co_scientist.task.outcome`, especially failed/retry_scheduled/lease_lost.
   The privacy exporter drops HTTP route/path attributes; GET spans also include
   SSE lifetimes, so their duration is not ordinary GET latency. Use service
   request logs and a local reproduction to identify slow read routes. In Sentry,
   watch increases in `Exception`, `TimeoutError` and worker failures;
   SQLite OperationalError and provider-specific timeouts can become generic
   `Exception`, and exported exception text is removed. Obtain detailed SQLite
   locked/timeout messages from scoped service diagnostics. Check process RSS/CPU,
   descriptor usage and queue age in service diagnostics. Expected 429s are load shedding.
   The local SQL wait/loop-lag adapter is not a production monitoring endpoint.
2. Keep one API replica and `RAILWAY_RUN_UID=0`. Keep `WORKER_POOL_SIZE=1`, the
   tested width. Do not add Uvicorn workers, replicas, serving VACUUM/checkpoints,
   or database writes on polling ticks to relieve traffic.
3. On read latency/resource pressure, reduce `SSE_MAX_CONNECTIONS` to 128 or 64.
   Restart through the documented release procedure: existing streams
   close and reconnect; refusals explain the delay. Lower `MAX_CONCURRENT_RUNS`
   to 1 to protect reads from new research work. Preserve existing durable
   tasks and provider transactions; do not delete the database or its WAL.
4. On quota pressure, lower `FREE_RUNS_GLOBALLY_PER_DAY`, `FREE_RUNS_PER_HOST_PER_DAY`
   and `FREE_RUNS_PER_DAY`; inspect `APP_LLM_GLOBAL_CALLS_PER_DAY`,
   `APP_LLM_GLOBAL_TOKENS_PER_DAY` and `PROVIDER_HOST_*` consumption. Daily counters
   reset on UTC boundaries. Raising free/admission/model budgets does not add
   API capacity and can spend the remaining allowance earlier. Preserve bounded
   call/token reservations and settlement. Resolve exhausted credentials or
   model availability using the existing provider runbook.
5. If ordinary API calls still fail at the tested limits, reduce intake and
   retain logs/trace IDs, rollback the offending release using [LAUNCH.md](LAUNCH.md),
   then reproduce locally at the observed mix. Capacity above the measured
   envelope requires another local load result; additional replicas require a
   store migration first. Alert configuration is in
   [MONITORING.md](MONITORING.md#alert-settings).

## Data and privacy

The service uses browser ownership identities, not email/password accounts. The
random `X-Client-ID` is a private capability; never ask a visitor to email it.

The inventory below follows the persisted schema, browser storage, provider
profiles and MCP implementations. Deletion of a live record does not erase a
copy already received by an outside service.

| Personal or user-supplied data | Storage | Retention | Outside recipients |
| --- | --- | --- | --- |
| Ownership ID, run/chat/document IDs, links and timestamps | Browser storage; SQLite owner columns | Browser until cleared; records with their work | Railway API; optional R2 database backups |
| Goals, generated titles/restatements, challenges, constraints, preferences, tier, configuration and sources | SQLite `runs`, interviews, tasks/checkpoints | Hosted `COSCIENTIST_RUN_RETENTION_DAYS=0` keeps runs until deletion; terminal default otherwise 90 days; drafts 7 days | Selected model provider; derived queries to research APIs |
| User and assistant chats, suggested answers, steering, Q&A, reasoning | SQLite `interviews`, `interview_turns`, `messages`, run events, task payloads/checkpoints | Standalone chats until deleted; run-bound copies with their run | Selected model provider, Railway, R2 |
| Uploaded title/filename, MIME type, size, hash, extraction metadata and extracted text | SQLite `staged_documents`; text copied into evidence/tasks/science outputs | Staged documents 30 days; copied text with its run/chat until deletion | Railway extraction; model providers; derived query words to research APIs |
| Original uploaded file bytes and extraction intermediates | Request memory and temporary parser/OCR files | Processing lifetime; original uploads are not retained | Railway; local parsers/OCR, not an external conversion service |
| Hypotheses, reviews, comparisons, citations, retrieval queries/results, full text, claims, plans, reports, errors | SQLite science tables, tasks, events and checkpoints | Run lifetime; may echo supplied text | Selected model provider, Railway, R2 |
| Prompt-cache text, run/role cache identifier and request partition | Request memory; up to 512 evidence baselines of at most 16,000 characters each in process RAM; Azure partition tracker limited to 4,096 run/role keys; external provider cache | Anthropic requests five-minute ephemeral caching; other provider retention follows its policy. Evidence baselines clear after committed run/data deletion or LRU eviction/restart. Up to 512 hashed erased-run markers block late memoization; filling that bound purges all baselines and disables memoization until restart. Local tracker entries last until eviction/restart; rolling 60-second call cleanup is not key expiry | Anthropic and Azure; Azure receives the run ID/role/optional partition in its cache key |
| BYOK worker/supervisor credentials, provider/model/custom-model choices | Browser local storage for keys and choices; encrypted SQLite `run_credentials`; request and brief validation memory | Browser keys and choices until removed or site data cleared; stored credentials until run or browser-identity deletion | Railway API; selected provider for catalog/probe/completion; R2 holds encrypted rows |
| Appearance, models, session view and pending creation/reload recovery | Browser local/session storage, listed below | Local until cleared; session until completion/removal/tab closes | Preferences used in requested API calls; no advertising recipient |
| Optional email in legacy data/configuration | SQLite legacy run configuration/tasks | With the run | Public completion email is disabled; legacy `notification.email` tasks complete without delivery |
| Feedback category/message, URL, run ID, supplied diagnostic export | SQLite `feedback` | 30 days, newest 200 entries and 10 MiB cap, enforced on submission | Railway/R2; maintainer, no external feedback-mail service |
| Browser/server diagnostic logs, route/interaction context, trace references | SQLite `app_logs`, bounded browser memory, stdout | Default capture cap 20,000 rows; browser 2,000; no fixed time expiry; owned logs erased on deletion | Railway stdout; a user-selected feedback export; R2 |
| Connecting-peer hashes, client/day/minute usage, reservations and idempotency receipts | SQLite admission ledgers and in-memory limits | Seven-day expiry, hourly bounded cleanup; live run host mapping with its run | Railway receives connection IP; R2 contains database counters |
| Numeric operator-funded charges, token bounds/usage, model/role/rates and random receipt IDs; the operator's Azure allowance versions (amounts, expiry, verified rates, note) | SQLite `llm_spend` and append-only `llm_azure_allowance`; current ownership link through `provider_token_reservations` | Funding ledger has no automatic expiry; admission link expires after seven days; spent money is not refunded | Railway/R2; no goal/document/email/key/owner-ID columns |
| Technical exception class/position, method and build metadata | Sentry SDK event projections | Sentry free/Developer: 30-day lookback; verify in the dashboard | Sentry; fresh projections remove arbitrary error messages, user/email, URLs, breadcrumbs, attachments, goals, documents and keys |
| Timing/outcome, trace/run/task IDs, numeric usage/budget/attempt metadata | Projected OTLP spans | Honeycomb events: 60 days from ingestion; verify in the dashboard | Configured tracing processor (Honeycomb EU); no prompt/document/email/key text or raw exception events |
| Website/API request IP and browser headers | Infrastructure access logs | Application logs: Railway Hobby 7/Pro 30 days; Workers Logs Free 3/Paid 7 days when enabled; other infrastructure records follow provider policy | Cloudflare and Railway |
| Entire SQLite database/WAL, including research, feedback and encrypted keys | Optional Litestream S3 replica in Cloudflare R2 | Policy: 30-day object expiry plus daily forced/verified snapshot refresh; lifecycle configuration and restore drill are required | Cloudflare R2 |
| Derived literature queries, topic/run identifiers and public papers | Query in request memory; public papers only in MCP `.public-papers-v1/shared` cache; owned retrieval copies in SQLite | Public-paper cache has no user content by design; legacy private query/run manifests are purged before MCP serves tools | Railway MCP; selected source APIs |
| Trailer request metadata | YouTube embedded player | YouTube’s privacy and storage policies | YouTube/Google when the player loads or plays |

### External recipients and model policy

Provider/model selection determines which service receives goals, chat context
and document excerpts. OpenRouter trial/free routes can retain prompts or use
them for training. Only the checked `qwen/qwen3.8-27b:free` profile pins ModelRun
with `only=["modelrun"]`, `zdr=true`, `data_collection="deny"`; do not extend
that promise to Ling, Nexpro, GLM, MiniMax, Nemotron, Gemma, Dots or fallbacks.
Inspect each actual `platform/llm/profile/` entry and request-routing policy
when changing a model. BYOK supports Anthropic, DeepSeek, Gemini, OpenAI and
OpenRouter/custom models under the chosen provider's policy. The hosted
Azure resource is in Sweden Central, but Global Standard inference is not a
guarantee that processing remains in the EU.

Provider prompt caching does not establish overall prompt retention. The
Anthropic adapter requests a five-minute ephemeral cache; Azure receives a
run-specific cache key containing the run ID, role and optional request
partition. The local Azure partition tracker is bounded to 4,096 run/role keys,
with entries retained until eviction or process restart. Numeric cache usage
may appear in stored call statistics and projected telemetry; prompt text and
cache-key text must not enter Sentry or exported spans. Source:
`platform/llm/request/cache.py` and `platform/llm/telemetry.py`.

MCP research requests may send supplied or derived search words/entities to
NCBI PubMed/PMC, OpenAlex, Europe PMC, arXiv, OpenCitations, ChEMBL, UniProt,
STRING, Reactome, Open Targets, Ensembl, gnomAD, GWAS Catalog and
ClinicalTrials.gov. Brave/Tavily are optional web-search recipients; opening
a source contacts its public publisher/site. Available science-skill tools
can add database recipients. A query can reveal personal information even
though the full uploaded file is not sent to these sources by design.

Hosting/diagnostic services are processors for contracted services; model and
research services can also process data for their own purposes. The operator
must confirm contracts, applicable adequacy decisions/transfer safeguards and
external retention. A European resource or dataset name alone
does not establish those facts. Do not submit confidential information,
personal data, medical records or credentials in research content.

### Browser storage

The application sets/reads no first-party cookies. All application storage
supports requested functionality; there are no advertising/analytics storage
keys. The landing trailer loads YouTube’s embedded player.

| Location | Exact key or prefix | Purpose |
| --- | --- | --- |
| localStorage | `co_scientist_client_id` | Private ownership identity |
| localStorage | `cosci-theme` | Chosen appearance |
| localStorage | `cosci-api-keys`, legacy `cosci-api-key` | BYOK provider keys until removed; legacy single key migrated and removed |
| localStorage | `cosci-api-provider`, `cosci-api-model`, `cosci-api-supervisor-model`, `cosci-api-custom-models` | Chosen providers/models and custom drafts |
| localStorage | `cosci:session-side`, `cosci:session-side:<runId>`, `cosci:session-tab:<runId>` | Chosen session view |
| sessionStorage | `cosci-logs-session-baseline` | Diagnostic session anchor |
| sessionStorage | `co_scientist_log_pause_until` | Necessary one-minute diagnostic refusal/reload recovery |
| sessionStorage | `co_scientist_pending_run_create:<chatId>` | Safe idempotent creation recovery |
| sessionStorage | `coscientist:chunk-reload-at` | Reload-loop guard |

Seven-day admission history expiry preserves current spent quotas and recent
settlement receipts. The monetary ledger keeps the deployment's charged total
without an automatic expiry. Clearing the ownership ID makes its server data
inaccessible to that browser.

Time-based retention runs hourly after API startup, outside port binding, with
short transactions. Never use VACUUM or truncating checkpoints from the server,
or hold a SQLite writer over network I/O.

The owner handles access, correction, restriction, objection and portability
requests, normally within one month, and explains any lawful limitation. The
Dutch supervisory authority is the Autoriteit Persoonsgegevens. When restoring
a backup, preserve erasure decisions and reapply deletions before serving old
work; restoring an older snapshot is not permission to revive erased content.

### Retention settings and external decisions

Run/draft/document settings are `COSCIENTIST_RUN_RETENTION_DAYS` (hosted 0,
default 90), `COSCIENTIST_DRAFT_RETENTION_DAYS` (7) and
`COSCIENTIST_DOCUMENT_RETENTION_DAYS` (30). BYOK needs the deployment's persistent
`BYOK_ENCRYPTION_KEY`; operator log access uses `LOGS_ADMIN_TOKEN`.
`LOG_CAPTURE_ENABLED=true`, `LOG_CAPTURE_LEVEL=INFO`,
`LOG_CAPTURE_MAX_ROWS=20000`, `LOGS_INGEST_PER_MINUTE=120` are code defaults.

`SENTRY_DSN`/`SENTRY_ENVIRONMENT` are server destination/environment;
`VITE_SENTRY_DSN` is the required frontend build destination (build MODE
supplies production environment). `OTEL_EXPORTER_OTLP_ENDPOINT` enables
traces; `OTEL_SDK_DISABLED=true` disables them.
`OTEL_EXPORTER_OTLP_HEADERS`/`OTEL_EXPORTER_OTLP_TRACES_HEADERS` hold exporter
credentials and are validated before SDK parsing. Never print secret values
or put operator/provider credentials into browser configuration.

Optional R2 backups require `LITESTREAM_R2_BUCKET`, `LITESTREAM_R2_ENDPOINT`,
`LITESTREAM_R2_ACCESS_KEY_ID`, `LITESTREAM_R2_SECRET_ACCESS_KEY` and a fresh
`LITESTREAM_R2_PATH` per deployment. `LITESTREAM_CONFIG` defaults to
`/etc/litestream.yml`; `COSCIENTIST_LITESTREAM_ACTIVE` is entrypoint-owned.
Litestream0.5.17 keeps restore anchors and last level files, so its default
24-hour snapshot retention is not a deadline for all old user data. Unchanged
database positions skip duplicate snapshots. An object lifecycle alone can
expire an idle database’s only restore base; pair expiry with a refreshed,
verified restore generation. R2 documents normal deletion within 24 hours of
object expiration, with possible delays. See [R2 lifecycle behavior](https://developers.cloudflare.com/r2/buckets/object-lifecycles/).

The hosted deployment runs backups with a 30-day object-expiry policy. The API
image forces and verifies a Litestream snapshot daily, even for an idle
database (`platform/db/backup_service.py`; [restore drill](RESTORE-DRILL.md)).
The R2 lifecycle rule is configured on the bucket, outside this repository;
verify it in the dashboard rather than from a source review. Reapply erasure
decisions before a restored database serves traffic. Litestream's default
retention alone is insufficient.

Provider documentation checked on 8 October 2026:

| Recipient / data | Documented period | Verification and limits |
| --- | --- | --- |
| [Sentry](https://sentry.io/pricing/), technical errors | Free Developer: 30-day lookback | MONITORING selects free; confirm project plan/configuration |
| [Honeycomb EU](https://docs.honeycomb.io/get-started/manage-costs/how-honeycomb-calculates-usage/), exported span events | 60 days from ingestion | Event/log retention, not the separate 13-month metrics policy; confirm actual dataset |
| [Railway](https://docs.railway.com/observability/logs), application stdout | Hobby 7 days; Pro 30 days (Free 3; Trial 7; Enterprise up to 90) | The selected plan is not established by source. Upgrading restores previously hidden history: do not promise physical erasure at the lookback deadline |
| [Cloudflare Workers Logs](https://developers.cloudflare.com/workers/observability/logs/workers-logs/), when enabled | Workers Free 3 days; Paid 7 days | Actual plan/enabled state needs confirmation; these are application logs, not all infrastructure/security records. Recheck pricing after 1 December 2026 |

Check dashboards, processor contracts and applicable transfer arrangements
whenever a provider, plan or processor changes, and update this table on any
mismatch. Do not invent a selected Railway/Workers tier or
claim configured periods from public plan documentation alone.

## Incidents

See [incident playbooks](INCIDENTS.md) for signals, first actions, rollback and
ownership, including the local [Litestream restore drill](RESTORE-DRILL.md).
