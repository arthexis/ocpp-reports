# Increment 2 — data-integrity cutover checklist

The `source_migration` utility creates **candidate copies only**. It never updates installed CSMS, Reports, or Forwarder databases. Do not use it during live ingestion: establish a maintenance window, stop CSMS/Reports ingestion and forwarding, and checkpoint WAL state before copying.

1. Verify the effective CSMS node configuration (hostname or explicit `/etc/ocpp-csms/node.toml`) will advertise the chosen canonical source ID after restart. Verify collector credentials are already keyed by the stable forwarding **node ID**, not by an evidence UUID.
2. Capture a validated rollback snapshot of CSMS schema-8 SQLite (including WAL state), Reports warehouse and Forwarder state. Record their source identity, source event cursor, changefeed sequence and acknowledged forward cursor. Confirm collector acknowledgement matches the forwarder and that pending changes are fully drained. Do not migrate while acknowledgements are pending.
3. On database **copies**, run `python -m ocpp_reports.source_migration --csms CSMS_SQLITE --warehouse WAREHOUSE_SQLITE --forwarder FORWARDER_SQLITE --output OUTPUT_DIRECTORY --old OLD_UUID --new gway-005 --node gway-005`. Outputs must not already exist.
4. Compare copied database row counts, event IDs, transaction identity/revisions, warehouse source cursor, replication sequence and forward acknowledgements against the originals. Confirm no destination-ID collision at the collector, and compare the collector's existing identity/cursor separately; this tool intentionally does not write collector state.
5. **Explicit operator authorization required for a real cutover.** Production replacement is not performed by the utility. Coordinate transactional replacement, rollback snapshots, matching configuration, and collector/CSMS acceptance with the local agent; all services stay stopped until verified.
6. Restart under operator supervision, verify no replay gaps or duplicate transactions, then verify collector and Reports report generation. Roll back all affected files/configuration together on mismatch. Never silently migrate or delete historical CSMS schemas on an ordinary deploy.

## Offline collector synchronization preflight (#79)

Stop producers, drain pending replication, then stop consumers and capture
coordinated SQLite backup snapshots of all four databases, including the central
collector independently. Preserve the original snapshots as rollback evidence.
Run this read-only command before the copy conversion:

```sh
python -m ocpp_reports.source_preflight \
  --csms CSMS_SNAPSHOT --warehouse REPORTS_SNAPSHOT \
  --forwarder FORWARDER_SNAPSHOT --collector COLLECTOR_SNAPSHOT \
  --old OLD_UUID --new gway-005 --node gway-005
```

The command requires the existing source binding, durable node-keyed ACK and
collector receipt. It rejects legacy collector UUID references, mismatched
cursors, pending replication, and missing or conflicting retained change records.
Collector `gway-005` history must exactly match the local UUID's history; it must
never be merged with unrelated history merely because cursor numbers match.
JSON payload formatting differences are allowed because the collector canonicalizes
payloads. Missing or pruned history requires separate review, not an assumed pass.
The JSON report records the source cursor, replication tip, ACK, collector cursor
and retained count, and explicitly reports `cutover_authorized: false`. Failure
returns a nonzero exit status without modifying inputs or creating databases.

This verifies snapshot replication agreement only. Independently verify the CSMS
export cursor is caught up, effective node configuration, credentials, warehouse
projections and collector latest state. Separate read transactions cannot prove
cross-host synchronization of live databases. A passing report does not establish
a maintenance window or authorize replacement. Keep all services stopped across
the final coordinated snapshots, conversion and verification; record a common
rollback boundary for all databases and configuration. Once forwarding resumes,
the collector can acquire new receipts: reverting only local files is no longer
a coordinated rollback. Reconcile those receipts before any rollback or retry.

## Other integrity invariants

- `sync_page` commits events, transaction observations, cursor and connector status through one ingestion transaction. A projection failure must roll back the page.
- The forwarding state `source_bindings` locks the stable node name to the observed evidence identity. Unexpected changes stop forwarding; the copy-only migration is an explicit conversion mechanism.
- Connector snapshots preserve last measured power and energy along with independent measurement timestamps, not simply the time of the latest status poll.
- The collector change log retains every accepted sequence and rejects conflicting replay before moving its acknowledgement cursor.
