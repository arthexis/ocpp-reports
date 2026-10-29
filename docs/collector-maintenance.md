# Repeatable collector source reset

This command is a local operator interface, never a network administration API.
It does not change credentials, TLS, diagnostic archives or CSMS history. Deploy
the reviewed hold-aware collector release to **all collector workers** first;
an older collector does not enforce the hold. Keep the prior release for rollback.

Use `ocpp-reports collector status|hold|reset|release` for a source-scoped reseed.
Every command requires an explicit database and source. Reset additionally
requires a new backup path and an existing hold; there is no global-reset option.
Commands emit sanitized JSON counts/cursors for operator receipts.

First coordinate the source: stop its forwarder, preserve verified CSMS,
local Reports and forwarder backups, rebuild only regenerable evidence, reset
the matching forwarder cursor/binding coherently, and verify retained historical
coverage. Leave forwarding stopped. Obtain any required charging maintenance
authorization. Do not infer these gates from a quiet collector.

For issue #97 specifically, wait for `FORWARDING_HELD` and `GWAY005_READY` from
Agent A. Then, on Watchtower:

```sh
sudo ocpp-reports collector hold --database /var/lib/ocpp-reports/warehouse.sqlite --source gway-005
sudo ocpp-reports collector status --database /var/lib/ocpp-reports/warehouse.sqlite --source gway-005
sudo ocpp-reports collector reset --database /var/lib/ocpp-reports/warehouse.sqlite --source gway-005 --backup /private/issue97/collector-before-reset.sqlite
```

The hold shares SQLite's writer lock with batch acceptance. Successful acquisition
drains already executing batches; later replication requests for that source
return authenticated HTTP 503 with Retry-After. Other sources and diagnostics
continue normally. Holds survive process restarts. Status is read-only.

Reset requires a held source and an existing recognized collector database. It
supports the known combined local-ingestion/collector schema and preserves all
local evidence, its replication journal and SQLite sequence allocator. A source
that also has locally ingested evidence requires a separate coordinated local
reseed; collector-only reset refuses it. Unknown tables, columns, triggers and
foreign keys still require schema review. While holding
the writer lock, it creates an exclusive mode-0600 SQLite backup, verifies full
integrity, schema/version and logical content, then removes only the specified
source's changes, latest projections and cursor (the durable ACK receipt). It
verifies other sources and schema are unchanged before committing. Validation
failure rolls back the deletion; the backup remains available. Use a private
backup directory and retain it through acceptance.

After an independent status check confirms zero source rows and a held source,
post `WATCHTOWER_READY` with sanitized counts, release and backup path. **Leave
the hold in place.** Only after a coordinator posts `RELEASE_FORWARDING`:

```sh
sudo ocpp-reports collector release --database /var/lib/ocpp-reports/warehouse.sqlite --source gway-005
```

Agent A starts its forwarder using the existing credential. Verify its first
batch and durable ACK, later progress, idempotent retry, historical coverage and
natural OCPP events independently at both ends before closing #97.

For a later reseed, repeat the coordination and hold/reset/release sequence
with the intended source ID and a **new backup filename each time**. Do not
automatically release or reset on a replication error. A previous reset receipt
does not satisfy a later operation's backup, readiness or authorization gates.

Before release, rollback may restore the verified collector backup while the
collector is stopped, preserving owner/mode and the hold-aware release. Never
reactivate an older, hold-unaware collector while relying on a hold. After
release, coordinate both hosts and reconcile post-release receipts before any
restore; restoring one side alone can discard newly acknowledged evidence.
