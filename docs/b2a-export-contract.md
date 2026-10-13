# B2A — CSMS export/v1 consumer contract

Source of truth: `arthexis/ocpp-csms` current `main`, `src/ocpp_csms/export_contract.py`, `transactions/contracts.py` and `tests/test_export_contract.py`. Fixture data here is synthetic and sanitized, **not a captured field export**.

## Envelope and source identity

The producer returns `{"schema":"ocpp-csms/export/v1","data":{...}}`. `data.source_id` identifies a CSMS evidence source, not a charger. Never merge cursor positions from distinct sources. `cursor.after` and `cursor.next` are nonnegative event IDs, and `cursor.more` signals another page. Events are ascending by ID and pages can be retried. Cursor exhaustion is **not** proof of historical period completeness. Runtime-only events do not advance this cursor.

## Mapping to reports summary/v1 (future B2C)

| CSMS export/v1 | Reports summary/v1 | Policy |
| --- | --- | --- |
| data.source_id | source_id | preserve unchanged |
| transactions[].id + charger_id | transactions[].id + charger_id | composite transaction identity |
| connector_id, rfid, state, active | same | nullable where producer permits; redact RFID for external outputs |
| started_at, stopped_at | same | offset-aware timestamps; producer uses receipt time for export transaction snapshots |
| energy_wh | energy_wh | Wh, not kWh; preserve null; reject negative deltas or classify missing, never clamp to zero |
| energy[].power_w | no direct transaction field | instantaneous W sample; do not confuse with Wh |
| energy[].energy_wh | no direct transaction field | meter register Wh sample, **not** a per-session energy delta |
| events[].at | source event time | received timestamp in producer event rows |
| cursor.after/next/more | ingestion provenance | cursor page evidence, not report period revision |
| data.status | no summary field | current appliance status, not historical period health |

CSMS `report/v1` is a separate operational report with different selection and energy units; it is **not** the B2 export input. Reporting intervals are half-open `[since, until)`, in UTC after IANA-local calendar boundary resolution.

## Compatibility and missing evidence

This consumer validates required envelope fields and known types; unknown additive fields are ignored. Unsupported schema versions, malformed cursors, timestamps without offsets, missing required collections, and nonnumeric measurements fail closed. JSON numbers in producer samples can be floating-point; summary/v1 currently requires nonnegative integer Wh, so B2C must explicitly choose a lossless conversion policy. Null meter endpoints remain null. The producer's `transaction_item` can expose negative meter deltas; do not silently present those as usable energy.

Snapshots include only transactions referenced by the current event page. B2B must deduplicate and reconcile snapshots across pages; B2C must define late revisions and a separate completeness proof before marking a report complete. No source database writes, CSMS imports, or SMTP side effects belong in B2A.

## Producer event shape

`event_record` emits `id`, `at`, `charger_id`, `kind`, `action`, and optional `connector_id`, `transaction_id`, `rfid` or action-specific status fields. It does **not** emit raw `payload`, `direction`, or `occurred_at` under those names. The sanitized fixture mirrors this normalized event shape.
