# CSMS export v1 consumer contract (B2A, issue #15)

Source of truth: `ocpp_csms.export_contract` on CSMS `main` (the producer is already merged). This document describes the intended **command output**, not a direct database interface. Reports must not read CSMS SQLite or JSON archives or depend on CSMS Python internals.

## Envelope and page
`schema: "ocpp-csms/export/v1"`, `data.source_id` (persistent string), `data.cursor: {after,next,more}`, `data.events`, `data.energy`, `data.transactions`, `data.status`, and `data.exported_at` (UTC producer observation time). Other envelope keys are preserved by the producer and should be accepted by the consumer.

`after` is exclusive; event IDs ascend, need not be consecutive, and `next` is the last event ID or `after` on an empty page. `more` indicates another event was visible. Consumer cursors are per source; advance only after durable successful ingestion. Event identity: `(source_id,id)`. Transaction identity: `(source_id,transactions[].id)`. Snapshot updates replace prior versions rather than adding their energy. Energy sample identity includes `(source_id,source_event_id,sample_index)` for the current producer shape.

## Units, time, quality
`energy_wh` is Wh, `power_w` is W. Display conversion to kWh/kW belongs to Reports. Unknown is null, not zero. `exported_at`, charger `*_event_at`, CSMS `*_received_at`, and Reports import/generation timestamps have distinct meanings. Additive evidence metadata normalizes timezone-aware event timestamps to UTC and maps invalid, missing or naive timestamps to null. Legacy transaction summary timestamps require producer-side UTC normalization (CSMS #268).

Transaction snapshot additive fields: `start_event_at`, `start_received_at`, `stop_event_at`, `stop_received_at`, `meter_sample_times` (payload/sample indexes and event/receive times), and `quality` booleans `active`, `incomplete`, `missing_meter`. Do not assume `recovered`, `inferred` or `corrected` are present in the current producer. Existing snapshot fields remain backward-compatible.

## Compatibility and known gaps
Accept additive unknown keys, reject unsupported major schema versions, malformed cursor shapes, impossible cursor regressions and invalid field types before committing any state. Replays must be idempotent. No `transaction_changes`, `changes_after`, second SQLite DB or forwarder.

Event-page transaction snapshots and status use a single SQLite read snapshot. Internal runtime/recovery events are not currently part of the OCPP event cursor; derived-only transitions cannot be assumed to generate a new export page. Backup rollback with unchanged source ID and cursor reuse is not yet detectable; do not claim reset safety without an explicit producer protocol.

## B2A fixture policy
Fixtures under `tests/fixtures/csms_export_v1/` are **sanitized contract illustrations**, not captured field output or proof of deployment. Replace/verify against the merged producer's actual JSON before declaring the contract frozen. Keep stable deterministic values and avoid real RFIDs or customer identifiers. Validate one active page, a later stop page, and an empty page with no cursor progress.
