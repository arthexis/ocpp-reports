# Increment 2 — data-integrity cutover checklist

The `source_migration` utility creates **candidate copies only**. It never updates installed CSMS, Reports, or Forwarder databases. Do not use it during live ingestion: establish a maintenance window, stop CSMS/Reports ingestion and forwarding, and checkpoint WAL state before copying.

1. Verify the effective CSMS node configuration (hostname or explicit `/etc/ocpp-csms/node.toml`) will advertise the chosen canonical source ID after restart. Verify collector credentials are already keyed by the stable forwarding **node ID**, not by an evidence UUID.
2. Capture a validated rollback snapshot of CSMS schema-8 SQLite (including WAL state), Reports warehouse and Forwarder state. Record their source identity, source event cursor, changefeed sequence and acknowledged forward cursor. Confirm collector acknowledgement matches the forwarder and that pending changes are fully drained. Do not migrate while acknowledgements are pending.
3. On database **copies**, run `python -m ocpp_reports.source_migration --csms CSMS_SQLITE --warehouse WAREHOUSE_SQLITE --forwarder FORWARDER_SQLITE --output OUTPUT_DIRECTORY --old OLD_UUID --new gway-005 --node gway-005`. Outputs must not already exist.
4. Compare copied database row counts, event IDs, transaction identity/revisions, warehouse source cursor, replication sequence and forward acknowledgements against the originals. Confirm no destination-ID collision at the collector, and compare the collector's existing identity/cursor separately; this tool intentionally does not write collector state.
5. **Explicit operator authorization required for a real cutover.** Production replacement is not performed by the utility. Coordinate transactional replacement, rollback snapshots, matching configuration, and collector/CSMS acceptance with the local agent; all services stay stopped until verified.
6. Restart under operator supervision, verify no replay gaps or duplicate transactions, then verify collector and Reports report generation. Roll back all affected files/configuration together on mismatch. Never silently migrate or delete historical CSMS schemas on an ordinary deploy.

## Other integrity invariants

- `sync_page` commits events, transaction observations, cursor and connector status through one ingestion transaction. A projection failure must roll back the page.
- The forwarding state `source_bindings` locks the stable node name to the observed evidence identity. Unexpected changes stop forwarding; the copy-only migration is an explicit conversion mechanism.
- Connector snapshots preserve last measured power and energy along with independent measurement timestamps, not simply the time of the latest status poll.
- The collector change log retains every accepted sequence and rejects conflicting replay before moving its acknowledgement cursor.
