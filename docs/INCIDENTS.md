# Incidents

Use the [operator pause and cancellation control](LAUNCH-CONTROL.md) with
the durable credit/admission policy. The deployment owner executes hosted
actions; these instructions do not authorize anyone else to change production.

The deployment owner is incident commander and the only person who changes hosted settings. Record start time, signal, chosen action, compatible rollback revision, and recovery checks in a private incident log; do not attach secrets, DB files, researcher text or raw error payloads. Use the operator token only in a private operator client. Keep exactly one API writer.

Pause new runs, continuations and chat replies first when an incident threatens capacity, correctness or spend. Default pause lets admitted work finish. Select cooperative drain only when further funded work or writes must stop; in-flight network outcomes can remain billable and must not be retried as if no dispatch occurred. Keep the visitor message factual and supply a return time only when known. Resuming admission is an explicit operator decision after recovery; a notice's estimated return time is not authorization to unpause.

## Free routes rate-limited or down

- **Signal:** Honeycomb provider-throttle/connection triggers, Sentry provider failures, admin queue/usage view showing parked tasks. `/health` may stay healthy because provider availability is not liveness.
- **First action:** Pause new work; inspect provider reset/retry-after and route availability without live model probes. Let existing bounded retries/parking obey admission. Publish the known reset time or “We will update this notice when service returns.” Do not add a paid fallback.
- **Rollback and recovery:** Restore the last validated zero-price profile/policy if the regression is config/code; otherwise wait for recovery and explicitly resume. Preserve quota reservations, parking timestamps, task attempts and completed tool results.
- **Who:** Owner.

## Azure spend faster than planned / credit exhausted

- **Signal:** The Azure budget alert, Azure Cost Management and the durable spend/reservation view; Honeycomb output-burn warning is a usage signal, not a currency quote.
- **First action:** Pause admission. Drain cooperatively if continued Azure dispatch would exceed the agreed budget. Inspect committed plus reserved/unknown spend, price version and expiry. Keep the Azure budget email as the only email alert; all other notifications use web/app.
- **Rollback and recovery:** Return to an approved available free-only configuration or the last reviewed budget policy; never increase the budget to clear an incident. Preserve the ledger and unknown outcomes. Resume only with an approved remaining allowance; publish no refill time unless Owner confirms it.
- **Who:** Owner.

## API crash loop or failed deploy

- **Signal:** UptimeRobot API-up failures, Sentry new/regressed error, `/health` non-200 and deployment/startup logs.
- **First action:** Stop repeated deploys; inspect the first failure and select the last known compatible release. Check API volume mount `/app/data`, `RAILWAY_RUN_UID=0`, one replica, required credential presence, and port/health configuration. Pause admission when the API is reachable.
- **Rollback and recovery:** Owner rolls back to the compatible image/config and verifies public `/health`, startup recovery and a local offline smoke before resuming. Code rollback does not undo schema/state; use an isolated backup restore if necessary. Do not restart multiple writers or copy the old production DB to the fresh launch project.
- **Who:** Owner.

## Volume near full

- **Signal:** UptimeRobot API-healthy keyword failure at HTTP 200, admin `/health` disk detail, filesystem free bytes and DB/WAL/checkpoint size.
- **First action:** Pause admission; inspect what is growing. Stop repeated logs/checkpoint churn. Remove only explicitly disposable caches/exports or run the supported retention/superseded-checkpoint cleanup in short transactions. Preserve kept runs and backup recovery copies.
- **Rollback and recovery:** Restore the prior retention/config if it caused deletion/churn; expand capacity under Owner's decision. If offline compaction is needed, stop all writers and make/verify a consistent backup first. Never VACUUM or truncate the WAL from the serving process, especially with Litestream active. Resume only above the configured disk floor with headroom.
- **Who:** Owner.

## SQLite locked or corrupt

- **Signal:** Sentry DB failures, admin health store/queue detail, write latency and repeated local locked errors; integrity check on an isolated stopped copy. A `SELECT 1` health check alone does not establish database integrity.
- **First action:** Pause new work. For locking, capture `py-spy dump` first and check writer transaction duration and accidental extra replicas; do not launch VACUUM/checkpoint retries. For corruption, stop writers, retain the DB and matching WAL/SHM privately, and validate a backup in scratch.
- **Rollback and recovery:** Roll back the offending writer change. For corruption replace only with a compatible, integrity-checked backup after all writers stop; retain old files, never mix sidecars, keep the matching BYOK encryption key, start one API and check recovery/ownership before admission. Use the local [restore drill](RESTORE-DRILL.md), then separately verify actual backup freshness and run/checkpoint/report data.
- **Who:** Owner.

## MCP down or sources refused

- **Signal:** API operator connector/probe detail and admin status; keyless MCP `GET /` manifest on its private network. UptimeRobot deliberately does not expose private MCP. Reports list failed sources as unknown, not no prior art.
- **First action:** Pause source-dependent new work; check the expected tools, actual MCP_SERVER_URL, TLS and matching shared-secret presence without displaying values. Distinguish missing/refused search/PubMed credentials from transport outage. Investigate connectivity/configuration; use safe manifest/status checks, not paid searches.
- **Rollback and recovery:** Owner corrects configuration and restarts MCP if needed. Roll back engine parser and server together only to a version containing the current status/records/error contract; if unavailable retain that contract and keep admission paused. Never disable TLS/auth or rewrite failed-source audit records.
- **Who:** Owner of the deployed MCP service.

## Abuse or spam runs

- **Signal:** Admin usage/admission counters and connecting-host refusals, Honeycomb token-burn/task volume, Sentry error spikes. Rotated browser client IDs do not establish independent people.
- **First action:** Pause admission; drain abusive work if needed. Inspect aggregate limits and operation/host/global reservations without exporting goals/IDs. Retain evidence minimally. Keep read/report access available while stopping funded actions.
- **Rollback and recovery:** Revert only the permissive admission change or restore the reviewed limits; do not clear the durable usage ledger or weaken ownership to unblock a caller. Resume when capped admission and the visitor notice are verified offline. Owner decides any external blocking policy.
- **Who:** Owner.

## A secret leaked

- **Signal:** Secret-scan finding, provider/account anomaly, Sentry or logs showing exposed credentials, or Owner report. Absence of telemetry is not proof no exposure occurred.
- **First action:** Pause and drain affected funded work. Owner revokes the affected credential immediately and preserves only redacted incident facts; inspect exposure and access privately. Rotate matching MCP secrets on both services and check auth. BYOK encryption-key rotation requires migrating or deleting affected encrypted credentials, never blind replacement.
- **Rollback and recovery:** Deploy the corrected secret boundary and use newly issued credentials; reverting leaked source alone does not revoke a secret. Remove/redact published exposure through the proper owner channel, review history and scope, verify no fresh leakage before resuming. Owner decides researcher notification obligations from established facts; no automatic email or invented legal promise.
- **Who:** Owner.

## Alert settings and recovery evidence

Use the exact web/app-only [alert settings](MONITORING.md#alert-settings). No additional email alerts; keep the Azure budget alert. The local Litestream 0.5.17 [restore drill](RESTORE-DRILL.md) exited 0 on 8 October 2026: 0.033 s restore, 3.008 s restore-to-healthy API. The expanded idle-backup proof verified a freshly uploaded complete base in 0.092 s after aging only its synthetic local replica by 31 days. This synthetic fixture does not establish production RTO/RPO or actual R2 backup freshness.

## Backup verification failed or stale

- **Signal:** Sentry's fixed backup-verification error, private operator verification status and last verified time; missing metadata is unknown. Investigate a failed refresh or no verified refresh for more than 25 hours. UptimeRobot catches an API stopped after an unexpected replication-daemon exit.
- **First action:** Keep new work paused; automatic failure pause lets admitted work finish. Owner checks volume headroom, R2 availability and the last complete base without exposing credentials or researcher data. Require a fresh forced snapshot and an integrity-checked scratch restore. Do not disable replication or clear the ledger to recover.
- **Rollback and recovery:** Use the last compatible release with working daily verified refresh; if none exists, keep admission paused until the refresh is fixed. Retain previous recovery files and the matching encryption key privately. A successful retry does not resume work: Owner verifies freshness and recovery, then explicitly resumes. Keep the approved 30-day policy; Owner alone changes hosted lifecycle settings.
- **Who:** Owner.
