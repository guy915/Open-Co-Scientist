# ADR-003: Durable runtime — keep the hand-built one

**Status:** Accepted.

## Context

Runs execute through a hand-built durable runtime: `scientific_tasks` rows
with leases, idempotency keys, retry budgets, parks and checkpoints, drained
by one worker cohort per run inside the API process. DBOS ships checkpointed
steps, durable queues and retries on SQLite, so a prototype ported the review
fan-out to DBOS (3.2.0, tables in the app's SQLite file, behind a flag, one
parent workflow per review commit and one child per hypothesis), and the plan
was to adopt it only if it passed seven checks.

## Decision

**Keep the hand-built runtime.** Three checks failed:

- **Shared SQLite file:** DBOS made about 118 write transactions per minute
  while idle (a timeout sweep and a queue thread), including a write on every
  poll tick, which the operational invariants in `docs/OPERATIONS.md` forbid.
- **Worker cohorts' event loops:** each cohort runs its own event loop
  (`platform/llm/scoped_loop.py`, `run_in_scoped_loop`). DBOS installs one
  process-wide executor as each loop's default; when a cohort's loop exits,
  every later DBOS call in the process fails.
- **Size:** the port was to delete at least 1.5k lines net, and saved about
  755 (range 580 to 900).

The other checks passed, with caveats: DBOS steps are at-least-once (a
mid-fan-out kill produced duplicate provider calls until the port added its
own issuance marker), and with two processes on one file DBOS recovery
re-executes live workflows.

**The runtime as built:**

| Part | Where |
|---|---|
| Queue, claim, lease renewal, expired-lease rescue, cohort polling | `orchestration/repository/tasks.py`, `tasks_lifecycle.py` |
| Worker cohort, enqueue and resume, success and failure outcomes | `orchestration/task_worker/` |
| Task execution per node, fan-out, gate, finalize | `orchestration/engine_tasks/`; its `runtime.py` holds the `EngineTaskRuntime` Protocol that keeps generators and asyncio primitives from outliving a cohort loop |
| Node table and channel reducers | `orchestration/task_runtime.py`, `registry.py`, `workflow_topology.py` |
| Tables and checkpoints | `platform/db/schema.py`, `platform/db/checkpoints.py` |

There is no single runtime interface: the `scientific_tasks` table is also
read by `platform/db/runs.py` and `orchestration/repository/runs_views.py`
(run status and listing queries) and by `engine_tasks/inputs.py` and
`support.py`. The queue's mutations stay in `orchestration/repository`.

## Consequences

- No new dependencies, no second scheduler on the SQLite file, and every
  invariant in `docs/OPERATIONS.md` keeps its implementation.
- What DBOS did better remains a known gap: recovery in seconds rather than
  after the lease expires, durable sleep for parks, and cancelling in-flight
  calls. Each would be a behavior change with its own benchmark.
- Revisit only if the store moves off single-writer SQLite or a library passes
  the idle-write, event-loop and size checks.
