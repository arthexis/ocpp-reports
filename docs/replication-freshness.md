# Replication freshness and connector status provenance (#111)

There are separate clocks. `sources.last_ingested_at` is **local ingest time**. `collector_receipts.received_at` is when a **new replication sequence** was accepted by the collector. A replay or no-op must not advance this value. `connector_state.observed_at` is the producer snapshot export time and can advance even if the charger remains faulted. `status_observed_at` is the original CSMS StatusNotification receipt, never inferred from exported_at, a Heartbeat, or a collector acknowledgement.

## Compatibility and installation

The new collector receipt table is additive and created by the existing collector schema initializer at first accepted batch. Historical receipts are **unknown** until a new sequence arrives; do not backfill from cursor positions or current wall clock. Existing connector rows lacking provenance remain unknown, and new exports may populate the timestamp from CSMS persisted original events. The schema and source IDs of CSMS are untouched. No database reset, source rekey, ACK reset, or manual historical rewrite is required.

Deploy the additive CSMS export producer change before expecting a reliable status age on newly ingested connector snapshots. Deploy Reports to the satellite (ingestion), then to Watchtower (collector and dashboard), preserving normal role assignments and existing customer source allowlists. During production validation, compare the same source/connector at two naturally separated export intervals: last status receipt remains fixed while the snapshot time advances. A genuine StatusNotification must update the original receipt time. Check Porsche shows remote receipt freshness for GWay-005 and ABB local ingestion freshness; Audi must remain scoped to GWay-002.

For rollback, restore the previous application release. Retain the additive `collector_receipts` table and payload metadata: older versions ignore them. Do not reset replication data, customer credentials, source mappings, or charger state. A fault, including EmergencyStop, is still the last known observation until superseded by actual newer evidence. Physical emergency-stop status requires an independent on-site check.


## Collector-owned charger locations (optional)

Location labels live in the **existing Watchtower collector warehouse** and are
not part of CSMS exports, source credentials, replication batches or immutable
evidence. Each assignment is keyed by `(source_id, charger_id)`; multiple
chargers may share a label and the same charger ID in another source is distinct.

Run these commands on the collector host with local database write permissions.
The database must already exist (the CLI never creates a new warehouse):

```sh
ocpp-reports locations list --database /var/lib/ocpp-reports/reports.sqlite3
ocpp-reports locations set --database /var/lib/ocpp-reports/reports.sqlite3 \
  --source gway-002 --charger CHARGER_ID --location 'Audi San Pedro'
ocpp-reports locations remove --database /var/lib/ocpp-reports/reports.sqlite3 \
  --source gway-002 --charger CHARGER_ID
```

The dashboard/API adds `location` to charger-specific connectors,
transactions and events and shows **Ubicación** in place of **Origen**.
Unassigned chargers display their original `source_id`. Synchronization
retains source identities. Customer source allowlists remain unchanged.
Deleting an assignment restores the source fallback; collector source reset
preserves assignments, while wiping/replacing the entire collector database
requires a separate metadata backup. Only the collector owner can edit labels,
and a viewer cannot edit them through the dashboard.
