# Launch admission control

The API persists one operator-controlled pause in its SQLite store. No deploy
or restart is needed. A configured `LOGS_ADMIN_TOKEN`, sent privately as
`X-Logs-Token`, is required to read or update the control. Browser identity,
loopback addresses and forwarded headers do not grant authority.

Open `/operations` on the site, enter the operator token and select **Load
control**. Select **Pause new work; let current work finish** for the default
pause, or the explicit cancellation option to stop current work as well.
Set one short visitor message and optionally a future return time in your
local timezone, then **Apply control**. The token stays only in page memory;
closing or navigating away clears it. Reload before acting on a conflict.
The shared visitor banner polls the read-only status every three seconds
while visible and refreshes on return to the tab. Stop/cancel remains usable.

`GET /api/launch-control` returns the current revision and the private numeric
credit snapshot. `PUT /api/launch-control` requires `expected_revision` from
that read. A stale revision returns 409; reload rather than overwriting
another operator's decision. These responses are not cached. Keep the token
in a private operator client, never a URL, browser storage or a posted command.

Example PUT body:

```json
{
  "paused": true,
  "drain": false,
  "message": "Research is paused while we restore free capacity.",
  "resumes_at": null,
  "expected_revision": 0
}
```

- `paused: true` refuses new runs, example copies, starts, continuations and interview/Q&A
  replies with 503. Reads, health, deletion, cancellation and operator control
  remain reachable. Guards recheck run creation/start/continuation inside
  their existing short admission transactions. Attachments, steering and chat
  inputs commit with admission; completed-run input and continuation queueing
  share one transaction. Replies admit before dispatch and retain that admission.
- `drain: false` lets admitted work finish, including an admitted reply.
- `drain: true` cooperatively cancels queued/running/synthesizing/parked runs,
  revokes their durable tasks and closes admitted chat producers. Workers
  check only a primary-key ownership scalar on their existing ≤1-second tick;
  renewal retains its own cadence. Silent replies observe drain within one
  second, with one producer task and bounded fragment backpressure.
- A cancellation can leave an already dispatched provider outcome billable.
  Charged/reserved money and unknown token reservations stay intact. Cancelled
  work is not automatically restarted on resume.
- `resumes_at` is an optional future Unix timestamp, shown in the visitor's
  local timezone. It is an estimate, never an automatic unpause timer.
- Resume explicitly with `paused: false`, `drain: false`, `resumes_at: null`
  and the latest revision. A persisted drain generation still closes an old
  reply even if an operator resumes immediately afterward.
  Guidance accepted during final publication but deferred by pause is retried
  on operator resume under the same capacity, provider and daily allowance checks.

The control writes only on an explicit operator update, using a short durable
transaction. A new store defaults to unpaused without inserting a row on
reads; restarts and backup restores retain a saved pause. No SQLite writer is
held over network I/O. Startup adds only the singleton schema, with no scan
or task recovery on the binding path.

`GET /api/launch-status` is public and read-only. It returns only the current
reason, public message, expected return instant, pause state, and whether
free and BYOK runs are allowed;
it exposes no researcher IDs, keys or balances. Pause reads are immediate.
The numeric credit aggregate is cached for at most three seconds to avoid a
lifetime-ledger scan per visitor. Free daily capacity returns the next UTC
midnight; current concurrency and exhausted/expired Azure credit have no
invented reset time. The ledger remains authoritative at physical dispatch;
this notice never reserves, refunds or authorizes a provider call. Remaining
free routes or the visitor's own key may still work after Azure is unavailable.

The authenticated operator endpoint has its own two-request buffer and a
4096-byte body limit. It does not consume anonymous visitor write allowances,
so a quota-exhaustion or erased browser identity cannot prevent unpause.

Offline regression coverage exercises token refusal, actual process restart,
expired ETA without auto-resume, all refused ingress paths, create/start
races, continuation transactions, read-only polls while the writer is held,
default reply completion, ContextVar isolation, silent reply/worker
cancellation, rapid resume, stale revisions, real quota/spend notices and
preservation of unknown paid reservations. No live provider call is needed.

Use the [incident playbooks](INCIDENTS.md), [exact web/app alerts](MONITORING.md#alert-settings)
and [local Litestream restore drill](RESTORE-DRILL.md) for recovery.

The private operator read also returns whether Litestream replication is enabled
and sanitized backup verification metadata: status, timestamps, transaction ID
and duration. `/operations` shows the last verified time; missing metadata is
unknown. A failed daily verification pauses new work without cancelling admitted
work and never invents a return time. A later successful verification does not
unpause. Follow the [backup playbook](INCIDENTS.md#backup-verification-failed-or-stale)
and [verified idle refresh drill](RESTORE-DRILL.md#daily-idle-backup-proof).
Public status never exposes backup metadata, paths or restored contents.
