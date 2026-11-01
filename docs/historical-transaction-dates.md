# Historical transaction date repair (issue #129)

`current_transactions` holds the local dashboard snapshot. Its immutable
`transaction_observations` retain the charger event timestamps and original
energy. Each projection update creates a new `replication_changes` sequence;
the forwarder sends it to the collector, which appends `collector_changes` and
replaces the matching `collector_latest` row. `sources.cursor` is the CSMS event
cursor, while `collector_cursors.sequence` is the durable replication cursor.
A corrected CSMS snapshot at an already consumed event cursor does not advance
either cursor by itself. Rewriting a consumed sequence would fail the collector's
conflicting replay check.

The `metadata dates` command repairs only `started_at` and `stopped_at` on
selected completed local projections, using `start_event_at` and
`stop_event_at` already held in a matching immutable observation. It keeps
energy, source and charger identity, events, observations and existing
replication payloads unchanged. The projection update creates a **new**
replication change. A later replay of the same historical CSMS export page
cannot undo the repair. No CSMS read or write, service restart, collector reset,
or forwarder hold is required for this append-only path. If an observation
lacks trustworthy charger timestamps, the command refuses it; investigate that
record and use the separately coordinated hold/reset/replay procedure only if
new evidence is needed.

## Operator procedure

Run on the local Reports host after CSMS #313 deployment. Use the actual
source ID and charge point ID from the authorized local inventory. The command
never guesses them. Dry-run is the default:

```sh
ocpp-reports metadata dates --database /var/lib/ocpp-reports/warehouse.sqlite \
  --source SOURCE_ID --cp CHARGE_POINT_ID --transaction 233 --dry-run
```

Review `current`, `proposed`, `energy_wh`, source and transaction identity.
For #233, `proposed` must show `2026-10-08T18:59:18.928Z` and
`2026-10-08T19:44:10.086Z`; energy must remain 34062 Wh. The original
receipt timestamps were `2026-10-22T00:21:47.137Z` and
`2026-10-22T00:21:47.654311Z`. Check #218–232 individually or pass repeated
`--transaction` options for a bounded reviewed batch. A record already correct
reports `unchanged`.

After review and explicit operator approval for the exact records, apply with a
new backup path in a private directory on the **local Reports** host:

```sh
ocpp-reports metadata dates --database /var/lib/ocpp-reports/warehouse.sqlite \
  --source SOURCE_ID --cp CHARGE_POINT_ID --transaction 233 \
  --backup /private/issue129/local-before.sqlite --apply
```

The command requires a mode-0700 backup directory, obtains a writer lock, makes and integrity-checks a mode-0600
SQLite backup before updating any projection, and refuses an existing backup
filename. Retain the backup and the JSON receipt. If a collector backup is
needed for operational rollback, take a separate SQLite online backup there
before forwarding the new sequence. Never copy a live SQLite file directly.

Verify local and collector dashboards with source allowlists: #233 must appear
on October 8 in UTC with duration 44 minutes 51.158 seconds, 34.062 kWh and
the correct source. Check #218–232, 20 rows per page, and **Exitosas**, 
**Fallidas**, **Todas** counts and selection. Confirm an unauthorized customer
dashboard cannot see this source. Check forwarder delivery and
`collector_latest` for the selected source and record key. Other source rows,
their cursors and dashboard visibility must remain unchanged.

## Rollback

Before forwarding, stop the local Reports follower/forwarder, restore the
verified local SQLite backup with the services stopped, then resume. This
discards any local Reports writes made after the backup, so reconcile them
first. After the collector has acknowledged the new sequence, coordinate both
hosts: hold the source and stop its forwarder, take fresh backups, reconcile
post-apply changes, then restore a consistent local/collector pair or issue a
new reviewed correction. Never restore only one side against a higher durable
ACK, and never edit `collector_changes` payloads directly. Keep CSMS and active
charging untouched.
