# #215: transaction timeline evidence and deferred collector replication

This document describes the draft implementation in PR #216. It authorizes **no** production changes, migrations, workflows, deploys or new OCPP traffic.

## Locally available evidence

- `energy_samples` is an immutable Reports-owned table keyed by `(source_id,event_id,sample_index)`; its payload is recorded by the existing ingestion path. Local `transaction_observations` identifies the earliest and latest **observed export cursor pages** for a specific `source_id,charger_id,transaction_id`.
- The read-only timeline first finds those cursor limits using the existing composite primary key; then uses the existing `energy_samples` primary key range `(source_id,event_id)`. It additionally requires *matching typed* charger, connector, transaction and `source_event_id/sample_index`, an offset-aware sample timestamp inside the transaction window, and valid measured values. At most 2,049 candidate rows are fetched from the scoped event range, and at most 120 real points are displayed.
- Cursor spans are not a proof of full historical completeness: the producer may omit a transaction from some export pages, or a partially imported warehouse may retain gaps. This page explicitly labels the observed window and partial coverage; absent proven cursor bounds yield an empty chart.
- Session energy is calculated only from actual non-negative cumulative meter readings and a recorded `meter_start_wh`. After the first downward counter jump the session energy curve is suppressed. If the bounded window is truncated, session energy is withheld rather than misrepresented as continuous. Power remains measured; nominal power is never substituted.
- SoC on `transaction.soc_end` is a last-known projection, **not** a time series. The current `ocpp-csms/export/v1` energy contract does not specify `soc` or `soc_provenance`. Accordingly, a SoC timeline is absent unless an individual immutable energy sample carries an explicitly verified `soc_provenance=ocpp-meter-values` field and a valid percentage. This is a conservative optional-extension rule, **not** certification that the exporter provides this field. Its exact producer attestation needs independent contract validation.
- No prior snapshot, final energy, status of C0, nominal rating, ingestion time, or collector receipt is backfilled into chart samples. No chart data is persisted by this draft.

## Collector limitation — separate future authorization required

Current replication triggers transmit `event` and latest `transaction` / `connector` / `rating` changes; `energy_samples` is **not** replicated as a historical series. `collector_latest` cannot reconstruct a session curve. A remote-only transaction therefore displays **Historial de mediciones no disponible**. A green PR CI does not certify the remote production dashboard can display measured charts.

A separate proposal is required before implementing any of the following:

1. Define a versioned, append-only, source-authorized sample replication contract using immutable `source_id,event_id,sample_index` identities, exact charger+connector+transaction, measurement timestamp and provenance; decide a bounded replay/backfill protocol and source/tenant authorization.
2. Determine retention, deduplication, cursor reconciliation, timestamp repair, index and capacity limits; explicitly distinguish a missing sample from a stale observation. Avoid putting the series into `collector_latest`, which retains only latest values.
3. Design collector schema and migration with rollback/compatibility tests. Do not silently alter active SQLite, relay, forwarder, or source CSMS.
4. Perform isolated integration and Android/desktop field validation only after explicit approval for protocol changes, migration, installation and access.

## CI evidence versus field validation

Hermetic Python tests exercise scope/cursor bounds and meter resets. The offline Chromium suite exercises the actual SVG plots at 320/360/390/520/1920 pixels in light/dark mode, for empty, singleton and multiple-sample series, plus an active 15-second poll. Synthetic browser fixtures cannot establish that the installed collector has real sample history. No customer screenshots need to be published or sanitized.
