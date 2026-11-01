# Historical SoC metadata refresh: reviewed one-transaction apply

The active CSMS is the evidence source. Reports only updates derived SoC fields on
an existing **completed** local transaction projection and emits one ordinary
transaction changefeed record. CSMS evidence, event IDs, observations and durable
ingestion/forwarder cursors are not rewound or rewritten.

Run on the node with the **local CSMS and Reports warehouse** for the selected
source, not on a collector-only Watchtower.

## Before applying

1. Confirm installed CSMS and Reports releases have the approved changes.
2. Record the expected source, exact charger ID and completed transaction ID.
   Confirm no active charging transaction uses the selected identity.
3. Create and verify an SQLite **online backup** of the Reports warehouse
   including committed WAL content. Record the local event cursor, transaction
   `imported_at`, relevant SoC values and source changefeed tip.
4. Preview (the default, read-only):

   ```sh
   ocpp-reports metadata refresh --database /var/lib/ocpp-reports/warehouse.sqlite \
     --source-id SOURCE --cp CHARGER --transaction TRANSACTION
   ```

   Review `fields_changed`, existing and proposed SoC, and `preview_token`.
   Stop on an unexpected value, source, CP, transaction, or conflict.
5. **Only after operator approval**, apply with the exact token from that
   preview:

   ```sh
   ocpp-reports metadata refresh --database /var/lib/ocpp-reports/warehouse.sqlite \
     --source-id SOURCE --cp CHARGER --transaction TRANSACTION \
     --apply --expect-preview PREVIEW_TOKEN
   ```

The token is a SHA-256 digest of the selected projection (raw payload,
fingerprint and original import timestamp), transaction identity, and producer
SoC endpoints. It is **not an authorization credential** and does not replace
operator approval or a backup. A changed producer value or projection refuses
the apply without writing; preview again and review the changes before retrying.
Unrelated source changefeed activity does not invalidate it.

The JSON receipt reports the selected identity, original `imported_at`,
existing/proposed endpoints, `fields_changed`, preview token and source
replication sequence before/after. Successful updates additionally report
`soc_metadata_refreshed_at`. Original `imported_at` is preserved; original
OCPP events and transaction observations remain unchanged. No-change applies
must not emit extra changefeed entries.

After applying, compare only the selected transaction projection and its
expected changefeed entry, then independently verify forwarder ACK and the
source-scoped collector projection. Check CSMS/Reports service health without
unnecessary restarts. Never reset cursors, replay events, or edit CSMS data to
force historical coverage. Keep receipts sanitized: do not post raw payloads,
RFIDs, tokens or database backups to public issues.

This is an **explicit one-transaction** procedure, not an automatic or bulk
historical backfill.
