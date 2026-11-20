# One-shot historical Arthexis transaction backfill (#266)

`python3 scripts/import_legacy_arthexis.py` is an **explicitly invoked**, standard-library-only converter. It reads archived Django/Arthexis SQLite using `mode=ro` and copies **historical transaction summaries** into the *existing* Reports local warehouse for that node. Its insert trigger creates normal `replication_changes`; the existing forwarder delivers those changes to the Collector under the **same `gway-005` or `gway-002` source_id**. No new service, timer, token, transport, source ID, CSMS reset or collector cursor is introduced.

## Observed sources and supported archives

| Node | Archive / path | Archive tag | Candidate rows | Known limitation |
|---|---|---|---:|---|
| `gway-005` | `/home/arthe/arthexis-backups/20261006-212723-retirement/db.sqlite3` | `primary` | 12 | 2026 retirement snapshot, `ocpp_metervalue` empty |
| `gway-005` | `/home/arthe/arthexis/backups/failover-20251120-1.sqlite3` | `failover-2025` | 11 | 2025 schema without `ocpp_transaction_id`; 2,296 recorded `ocpp_metervalue` **not imported by this utility** |
| `gway-002` | `/home/arthe/arthexis/db.sqlite3` | `primary` | 229 | 149 lack stop timestamps, 7 exact duplicate summaries, many missing meter bounds, `ocpp_metervalue` empty |

The current Reports warehouses had 8 current CSMS transactions for `gway-005` and 17 for `gway-002` during field investigation. Those live CSMS projections are immutable to the importer.

Importer maps archived Django PK to `1_000_000_000 + PK` (`primary`) or `2_000_000_000 + PK` (`failover-2025`), **scoped to the existing node source ID**. Original OCPP transaction IDs may be reused. A mismatch in charger serial, physical connector, schema, archive year or immutable previously imported row fails closed. Exact duplicate summaries within an archive and already-imported identical evidence are not added again. Overlap with current CSMS records or different evidence with an identical start fails closed. All other sources/tenants remain untouched.

Historical Django uses `USE_TZ=True`; stored SQLite naive datetimes are UTC. Meter register differences are in Wh (the legacy transaction model divides meter values by 1,000 for reporting). Missing readings, negative counters, absent StopTransaction, or unsupported data must never be converted to fabricated energy. An incomplete session is inactive, has `energy_wh=null`, and is never treated as an active or successfully completed charge. Sensitive fields (VIN, RFID, customer/account, auth, phone, payment info) are intentionally omitted. `historical_provenance` preserves archive tag, table, row PK and original OCPP transaction identifier for audit. The importer **does not create temporal energy, SoC or MeterValues samples** or falsely offer historical graphs. A separate provenance-validated adapter may import the actual 2025 samples after their foreign-key and timestamp relationship is verified.

## Dry run (never mutates either database)

Use the *reviewed branch/release* script on each node and an identity that can already read its protected Reports warehouse, normally the existing administrative sudo mechanism. Do not change ownership or permissions of the databases.

```bash
sudo python3 scripts/import_legacy_arthexis.py \
  --legacy-db /home/arthe/arthexis-backups/20261006-212723-retirement/db.sqlite3 \
  --warehouse /var/lib/ocpp-reports/warehouse.sqlite \
  --source-id gway-005

sudo python3 scripts/import_legacy_arthexis.py \
  --legacy-db /home/arthe/arthexis/backups/failover-20251120-1.sqlite3 \
  --archive-tag failover-2025 \
  --warehouse /var/lib/ocpp-reports/warehouse.sqlite \
  --source-id gway-005

sudo python3 scripts/import_legacy_arthexis.py \
  --legacy-db /home/arthe/arthexis/db.sqlite3 \
  --warehouse /var/lib/ocpp-reports/warehouse.sqlite \
  --source-id gway-002
```

A dry run emits source, archive tag, charger serial, `old_rows`, `projected`, `to_insert`, `exact_duplicates_skipped`, `previous_archive_duplicates`, `already_present`, `completed`, `incomplete`, `unknown_energy`. These are **candidate rows**, not proof that each represents an independent successful charging session. Review source-specific counts and current CSMS bounds before authorizing writes.

## Apply — only after separate operational acceptance

Dry-run and CI acceptance do **not** authorize this step. Record original warehouse/collector/forwarder cursors, source allowlists and SQL counts, and confirm enough disk space. Ensure the chosen backup directory is private and durable. Supply the same args as dry-run plus `--apply --backup /private/path/unique-before-import.sqlite3`. The backup filename must not already exist; SQLite's online backup API takes a consistent snapshot, with integrity check. A single `BEGIN IMMEDIATE` transaction applies new projections and their normal changefeed triggers. No collector or CSMS database is ever opened for writing and no cursor or ACK is reset.

Wait for the **existing** forwarder to acknowledge the new journal rows, check collector `collector_latest` by `(source_id,kind='transaction')`, confirm the projected sessions appear in the administration dashboard, and test tenant/customer source allowlists. No one should grant new source visibility on the basis of the importer. A rerun with identical evidence is a no-op; conflicting evidence aborts. Preserve the source snapshots and backup for rebuilds. Do not restore an old backup over a live warehouse after forwarding without coordinated reconciliation of acknowledged changes.

If **only the central Collector** is rebuilt, rerunning the importer does not replay already-forwarded journal sequences. Use the existing source-scoped collector hold/reset + coordinated forwarder reseed procedure in `docs/collector-maintenance.md`. If both local Reports and Collector are rebuilt, reimport from the preserved Arthexis databases. Never silently alter cursor sequence or fabricate event history.

## Tests

```bash
python -m pytest tests/test_import_legacy_arthexis.py -q
```
