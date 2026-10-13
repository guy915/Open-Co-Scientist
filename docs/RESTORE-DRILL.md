# Local Litestream restore drill

Run this before relying on backups and after changing the backup format or API startup.
It creates a synthetic application database, backs it up to a local file
replica, restores into a separate scratch file and starts one offline API on
loopback. It never opens an existing database or an R2 replica.

A production restore records an Azure spend hold, because a replica can trail
the money ledger; see [Azure setup](azure-setup.md#spending-cutoff) before
acknowledging it.

## Procedure

Run `make setup` with Bun 1.3.14 first. Obtain Litestream **0.5.17**, the
version pinned in `Dockerfile.api`, for your host from the
[release](https://github.com/benbjohnson/litestream/releases/tag/v0.5.17).
Verify its archive against the release's `checksums.txt` before extracting.
The Linux x86_64 archive used for this drill has SHA-256
`cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d`.

From the repository root, with the absolute path to that binary:

```sh
.venv/bin/python scripts/operations/restore_drill.py --litestream /absolute/path/litestream
```

The command creates a private `cosci-restore-drill-*` directory under the
system temporary directory. Use `--scratch-parent /absolute/scratch/directory`
to choose another parent. It preserves the fixture, local replica,
configuration, API log and JSON receipt there for inspection.

The script performs these steps, stopping on an error:

1. Initialize the actual application schema and a synthetic marker in
   `source.db`. No research task is queued.
2. Use the production refresh helper to run `litestream replicate -config
   <scratch>/litestream.yml -once -force-snapshot`, with a file replica and
   `truncate-page-n: 0`. Verify the restore plan selects a newly uploaded
   complete level-9 snapshot, restore that transaction ID privately and check
   SQLite integrity. Age only this synthetic replica's complete base by 31 days,
   then repeat with no application database write. Assert the transaction ID
   stays unchanged, the base's modification time is refreshed, and recovery
   verifies again. Temporary verification copies are removed on success/error.
3. Start the recovery timer and run
   `litestream restore -txid <verified-id> -o <scratch>/restored.db
   file://<scratch>/replica`.
   Verify `PRAGMA integrity_check` and the original marker using a read-only
   connection.
4. Start one API with the restored database on an ephemeral 127.0.0.1 port.
   The child runs in scratch with an environment allowlist: forced offline
   execution, LiteLLM's local-only cost catalog, disabled tracing and no
   inherited provider, SMTP, Sentry, proxy or Python-path settings. No
   repository dotenv file is loaded: `PYTHON_DOTENV_DISABLED=1` disables
   python-dotenv's module-relative search and the settings dotenv reader.
   SQLite application checkpoints remain disabled as under Litestream.
5. Poll `/health` until healthy, with a 60-second readiness deadline. Record
   restore-only and restore-to-healthy elapsed time.
6. Stop the API, verify integrity and the marker again, then publish
   `receipt.json`. Failed startup leaves the scratch logs and no success
   receipt. The script terminates its child even when readiness fails.

Inspect the receipt and logs before deleting only the reported scratch
directory. Never substitute the production volume or an existing database
for the synthetic source or restored target.

## Measured receipt

On 8 October 2026, Linux x86_64, Litestream 0.5.17, this procedure exited
**0**. Restore took **0.033 seconds**; restore through healthy API took
**3.008 seconds**. SQLite integrity was `ok` before and after API startup and
shutdown, and the original marker survived. No provider call, production
data access or hosting change occurred.

This small fixture validates the local restore/startup procedure. It does
not measure production RTO, backup freshness/RPO, R2 credentials/network
availability, researcher ownership, encrypted BYOK recovery, checkpoint
compatibility or full-sized database recovery. An operator restoring an
actual backup must separately verify those against an isolated, compatible
release before replacing any store; see
[backup and restore](LAUNCH.md#backup-and-restore). Keep the database and its
matching encryption key private. Stop all writers for actual replacement,
retain the old database and sidecars together, never pair a restored DB
with another snapshot's WAL/SHM, and restart exactly one API writer.

### Daily idle-backup proof

The expanded drill on 8 October 2026 exited **0** with Litestream 0.5.17.
Both forced uploads used transaction ID `0000000000000001`; no application
data changed between them. The deliberately 31-day-old complete local base
was reuploaded and selected for an integrity-checked restore. The second
force/upload/verification took **0.092 seconds**, final restore **0.033 seconds**,
and restore through healthy offline API **3.838 seconds**. The marker and
integrity survived startup/shutdown. This remains synthetic local evidence,
not a measurement or assertion about any R2 account.

When all four R2 settings are present, the entrypoint starts the backup
supervisor. It starts the API independently of replication, then forces and
verifies a full snapshot at startup and every 24 hours. Only the replication
daemon is stopped during each bounded refresh; API writers keep serving.
The supervisor never runs two Litestream writers or holds an application
write transaction across replication, restore or integrity checking. Each
command has a 120-second timeout. Verification uses a private temporary
directory on the database volume and removes its copy and sidecars.

A failure preserves the last verified timestamp, records sanitized metadata,
pauses new work without cancelling admitted work, and retries after one hour.
Success never unpauses an operator decision. An unexpected daemon exit stops
the API rather than continuing without replication. The operator checks failed
verification via the operator view and the fixed Sentry error when configured,
checks volume headroom and R2 availability, then explicitly resumes only after
a fresh verified snapshot. No restored database or child diagnostic payload is
sent to Sentry. The status file is private, atomic, and contains timestamps,
transaction ID and duration only; supervisor polls do not write the database.

The hosted deployment uses a 30-day R2 maximum-age lifecycle. The operator
configures and verifies that rule and the notification destinations; this code
does not change R2 or any hosting account. Do not rely on backups until the
rule, a fresh verified base and matching encryption-key recovery are established.
