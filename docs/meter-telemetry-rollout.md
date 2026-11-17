# MeterValues telemetry: additive export and append-only replication

This is an **operator-run rollout plan**, not authorization to touch an active
CSMS, Collector, Forwarder, or its cursors. Tracks #218, Reports #227/#228,
and CSMS #376. Do not run `--apply` based on a CI result alone.

## Data and provenance

* Source: timestamped OCPP 1.6J `MeterValues` only; supported channels are
  `SoC` (%), `Voltage` (V), `Current.Import` (A),
  `Current.Offered` (A), and `Temperature` (Celsius).
* Power is only `Power.Active.Import` with an approved unit. Never calculate
  kW from V×A without phase/AC/DC evidence. Never label EVSE temperature as
  battery temperature. Do not infer unreported channels, phases or timestamps.
* Each base sample is immutable and scoped by source, OCPP event ID, sample
  index, charger, connector, and transaction.
* Distinct phase-specific channels are stored without conflating them into a
  single scalar. The dashboard shows scalar values only when unambiguous.
* Session-level SoC endpoints **do not substitute for historical SoC samples**.

## Deployment order and compatibility

1. In the intended **Collector**, install the Reports release containing
   `collector_meter_samples`, `collector_meter_enrichments`, and the
   capability-aware receiver. Verify DB integrity and readiness using its
   existing source-specific bearer. Old readiness clients remain v1-compatible.
2. Upgrade the corresponding Reports **Forwarder/local ingestion** on each
   enrolled source, without resetting or editing its source binding or ACK.
   Fresh `meter_sample` journaling stays **disabled by default** on upgrade.
   After verifying Collector capability, preview and separately approve
   `history_queue --enable-future --apply` for each intended local CSMS UUID.
   For `meter_sample` and `meter_enrichment`, Forwarder requires explicit
   `supported_kinds` from Collector; otherwise transmission stops **without
   advancing** the confirmed cursor.
3. Upgrade the CSMS producer to the additive `export/v1` metering contract.
   Retain the original OCPP events and status/configuration unchanged.
4. Verify per-source counts, sample identity, tenant isolation, successful
   duplicate replay, collector ACK, and dashboard active/historic endpoint
   behavior before enabling the next source.
5. Backfill is an independent operator approval, not automatic on upgrade.
   Collectors running an older release must not receive newly queued meter
   kinds. Preserve exact backups and rollback receipts before any apply.

## Dry-run previews (no writes)

Use **the local Reports warehouse** on the source host, not the remote
Collector DB. Replace the paths and source UUID with verified local values.

```bash
python -m ocpp_reports.replication.history_queue \
  --database /path/to/reports-warehouse.sqlite \
  --source-id EXACT_LOCAL_CSMS_SOURCE_UUID --limit 250
```

Before turning on ongoing sample replication, preview the exact source:
```bash
python -m ocpp_reports.replication.history_queue \
  --database /path/to/reports-warehouse.sqlite \
  --source-id EXACT_LOCAL_CSMS_SOURCE_UUID --enable-future
```
Only after verifying the upgraded Collector's authenticated capabilities and
receiving operator approval, repeat with `--apply`. Existing records do not
silently enqueue when future collection is enabled.

This reports `scanned`, `pending`, `next` and `more` without creating any
tables or journal rows. Continue using the composite `next.event_id` and
`next.sample_index` values; never advance the page cursor by event alone.

For historical SoC/other channels, obtain a **bounded, re-exported CSMS
export/v1 JSON page** with exact source/event IDs using the active CSMS
producer. Do not export unrestricted raw OCPP JSON or credentials.

```bash
python -m ocpp_reports.warehouse.history_enrichment \
  --database /path/to/reports-warehouse.sqlite \
  --source-id EXACT_LOCAL_CSMS_SOURCE_UUID \
  --page /path/to/bounded-export-v1-page.json --max-samples 250
```

The preview verifies immutable base sample identity, event index, timestamp,
energy, power, current warehouse source, and measured-channel provenance.
`cursor_unchanged` is reported; only a separate explicit operator approval
may authorize `--apply`. Both commands can accept `--apply` **only after**
preflight and compatibility checks. No forwarder/Collector ACK manipulation,
historical OCPP replay, or SQLite UPDATE of original `energy_samples`.

## Failure and rollback

* Missing metadata, inconsistent base samples, invalid units, unexpected
  phases or source identity, duplicate mismatch, timestamps outside the
  session, and Collector capability mismatch: **stop without mutation/ACK**.
* A documented meter reset must break the energy curve; incomplete coverage
  must remain visible, never interpolated or presented as billable consumption.
* On failure after an append, keep the journal and sample bytes immutable.
  Recover by bringing Collector and Forwarder back to a compatible version,
  not by deleting sample rows or rewinding cursors.
* Keep #218 open until integrated staged rollout, backfill approval, source
  isolation, restart recovery and real remote history acceptance are complete.
