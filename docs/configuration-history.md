# GetConfiguration history in OCPP Reports

Read-only evidence only. The browser never sends an OCPP command or contacts CSMS.

## Data flow

CSMS records the OCPP GetConfiguration response in SQLite. Its `export/v1`
contract adds sanitized `configuration_snapshots` to the matching event
pages; generic events still omit the raw OCPP payload. Reports validates
each inbound GetConfiguration event identity, stores the snapshot under
`(source_id, source_event_id)` and creates a `config_snapshot` journal
entry. Collector preserves every distinct snapshot in
`collector_configuration_snapshots`, instead of replacing earlier versions.

Coverage is `full`, `partial` or `unknown`; uncertain matching never
asserts full coverage. Values of known sensitive keys must be
`[REDACTED]` before leaving CSMS. Reports rejects unredacted known secrets.

## Safe deployment order (not authorized by this document)

1. Roll out a Collector advertising authenticated `config_snapshot` support.
2. Deploy compatible Reports importer, Forwarder and dashboard.
3. Deploy CSMS exporter.
4. Validate end-to-end in a nonproduction canary before production acceptance.

Forwarder and Relay check destination capability before sending this kind.
Deploying producers first would block replication to older collectors.

## Explicit bounded historical backfill

Only after the compatible consumer is deployed, on the source node:

```bash
ocpp-csms export --config-history --after 0 --limit 100 -j > config-history-page.json
ocpp-reports config-history import --database /path/to/reports.sqlite3 --page config-history-page.json
# Review the dry-run, then repeat the import with --apply.
```

Use `data.cursor.next` as the next `--after` value and continue while
`data.cursor.more` is true. Backfill is immutable and idempotent.
It does not change the normal CSMS or Reports event cursors. Only new
configuration snapshots produce ordinary replication journal entries.
Handle temporary JSON pages as private operational files.

## Dashboard

Admin-only route:
`/chargers/{source_id}/{charger_id}/configuration`.

The right-side header button appears only if this precise device has
at least one stored snapshot. Captures are ordered by original source
event ID and paginated; select a historical version, search the visible
keys, and see readonly flags, unknown keys, coverage and source timestamp.
No configured charger changes are made by this feature.
