# Watchtower source reset for issue #97

This command is a local operator interface, never a network administration API.
It does not change credentials, TLS, diagnostic archives or CSMS history. Deploy
the reviewed hold-aware collector release to **all collector workers** first;
an older collector does not enforce the hold. Keep the prior release for rollback.

Wait for `FORWARDING_HELD` and `GWAY005_READY` from Agent A. Check its consistent
backups, retained historical coverage and maintenance authorization in #97.
Do not infer these gates from a quiet collector. Then, on Watchtower, using the
installed release's Python and source path:

```sh
python -m ocpp_reports.collector_admin hold --database /var/lib/ocpp-reports/warehouse.sqlite --source gway-005
python -m ocpp_reports.collector_admin status --database /var/lib/ocpp-reports/warehouse.sqlite --source gway-005
python -m ocpp_reports.collector_admin reset --database /var/lib/ocpp-reports/warehouse.sqlite --source gway-005 --backup /private/issue97/collector-before-reset.sqlite
```

The hold shares SQLite's writer lock with batch acceptance. Successful acquisition
drains already executing batches; later replication requests for that source
return authenticated HTTP 503 with Retry-After. Other sources and diagnostics
continue normally. Holds survive process restarts. Status is read-only.

Reset requires a held source and an existing recognized collector database. It
rejects unknown tables, columns and triggers for schema review. While holding
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
python -m ocpp_reports.collector_admin release --database /var/lib/ocpp-reports/warehouse.sqlite --source gway-005
```

Agent A starts its forwarder using the existing credential. Verify its first
batch and durable ACK, later progress, idempotent retry, historical coverage and
natural OCPP events independently at both ends before closing #97.

Before release, rollback may restore the verified collector backup while the
collector is stopped, preserving owner/mode and the hold-aware release. Never
reactivate an older, hold-unaware collector while relying on a hold. After
release, coordinate both hosts and reconcile post-release receipts before any
restore; restoring one side alone can discard newly acknowledged evidence.
