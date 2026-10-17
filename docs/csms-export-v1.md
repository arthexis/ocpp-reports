# CSMS export v1 consumer contract (B2A, issue #15)

Source of truth: `ocpp_csms.export_contract` on CSMS `main` (the producer is already merged). This document describes the intended **command output**, not a direct database interface. Reports must not read CSMS SQLite or JSON archives or depend on CSMS Python internals.

## Envelope and page
`schema: "ocpp-csms/export/v1"`, `data.source_id` (persistent string), `data.cursor: {after,next,more}`, `data.events`, `data.energy`, `data.transactions`, `data.status`, and `data.exported_at` (UTC producer observation time). Other envelope keys are preserved by the producer and should be accepted by the consumer.

`after` is exclusive; event IDs ascend, need not be consecutive, and `next` is the last event ID or `after` on an empty page. `more` indicates another event was visible. Consumer cursors are per source; advance only after durable successful ingestion. Event identity: `(source_id,id)`. Transaction identity: `(source_id,transactions[].id)`. Snapshot updates replace prior versions rather than adding their energy. Energy sample identity includes `(source_id,source_event_id,sample_index)` for the current producer shape.

## Units, time, quality
`energy_wh` is Wh, `power_w` is W. Display conversion to kWh/kW belongs to Reports. Unknown is null, not zero. `exported_at`, charger `*_event_at`, CSMS `*_received_at`, and Reports import/generation timestamps have distinct meanings. Additive evidence metadata normalizes timezone-aware event timestamps to UTC and maps invalid, missing or naive timestamps to null. Transaction summary timestamps are normalized to UTC or null by the producer (merged CSMS #268).

Transaction snapshot additive fields: `start_event_at`, `start_received_at`, `stop_event_at`, `stop_received_at`, `meter_sample_times` (payload/sample indexes and event/receive times), and `quality` booleans `active`, `incomplete`, `missing_meter`. Do not assume `recovered`, `inferred` or `corrected` are present in the current producer. Existing snapshot fields remain backward-compatible.

## Compatibility and known gaps
Accept additive unknown keys, reject unsupported major schema versions, malformed cursor shapes, impossible cursor regressions and invalid field types before committing any state. Replays must be idempotent. No `transaction_changes`, `changes_after`, second SQLite DB or forwarder.

Event-page transaction snapshots and status use a single SQLite read snapshot. The current CSMS design (issue #244, superseding earlier recovery proposals) treats SQLite OCPP evidence as authoritative and preserves incomplete transactions without synthesizing StopTransaction or recovery transitions. A late real StopTransaction is a new event and its transaction snapshot is exported on the event cursor. Query-time inferred boundaries are not source events; they must not be exported or treated as proof of completion. No separate transaction-change cursor is planned. Backup rollback with unchanged source ID and cursor reuse is not yet detectable; do not claim reset safety without an explicit producer protocol.

## B2A fixture policy
Fixtures under `tests/fixtures/csms_export_v1/` are **sanitized contract illustrations**, not captured field output or proof of deployment. Replace/verify against the merged producer's actual JSON before declaring the contract frozen. Keep stable deterministic values and avoid real RFIDs or customer identifiers. Validate one active page, a later stop page, and an empty page with no cursor progress.

## B2B3 current transaction projection

`ocpp_reports.transaction_projection.current_transactions(reports_db, source_id=None)` reads only Reports-owned SQLite and returns one latest observed transaction snapshot per `(source_id, charger_id, transaction_id)`, with source and observation provenance. Higher export page cursor wins; within the same cursor, the latest distinct stored observation wins. Replaying an older page cannot overwrite a later cursor's snapshot. All original observations remain stored for audit; energy from different revisions is never summed. The projection does not declare a transaction physically complete or make a report period final. This is a read-only query, not yet a warehouse-backed `generate` command.

Current v1 cannot reliably detect same-source backup rollback with reused event IDs. That limitation must be handled by an explicit producer reset protocol in a separate change, not inferred from an empty page.

## B2C offline report generation from Reports SQLite

After importing export pages into a Reports-owned SQLite database, use:

```sh
ocpp-reports generate --source reports.sqlite3 --source-id fixture-source-a \
  --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z \
  --timezone UTC --format csv --output report.csv
```

`--source-id` is optional only when the database contains exactly one source with observations. The command reads Reports SQLite without modifying it; the existing JSON-page `--source` path is unchanged. The generated revision is a digest of selected current observations and their stable evidence fingerprints, excluding volatile import timestamps. A late StopTransaction produces a new revision; missing Wh remains null, never zero. Non-completed/active transactions remain partial. This is not yet a guarantee of source completeness or a multi-source warehouse report.
