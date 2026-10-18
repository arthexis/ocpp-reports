# B2C cursor ingestion acceptance and limits

Reports imports CSMS `ocpp-csms/export/v1` pages using one event cursor per `source_id`. Each page is validated, and its events, energy samples, transaction observations, current projection and cursor are committed together. Replays must not advance or rewind a cursor; transaction energy snapshots replace earlier snapshots rather than being summed.

## Acceptance cases

1. **Pagination and sparse IDs:** `more=true` permits a subsequent page; event IDs may have gaps, and an empty terminal page leaves the cursor unchanged.
2. **Late or out-of-order event time:** event arrival/cursor position determines observation precedence, not the charger event timestamp. A genuine late stop can update the current transaction projection.
3. **Same-source rollback:** an event ID reused with different payload is rejected. **A reset or restored producer with identical/absent historical IDs cannot always be detected**; the v1 protocol does not carry a source generation identifier or guarantee reset detection. Do not claim lossless recovery. Operator intervention/new source identity or a future contract revision is needed.
4. **Read-only reports:** generating a report from Reports SQLite must not mutate evidence, projections, or cursor. `run` must not perform implicit ingestion.
5. **Restart/retry:** successive ingestion calls use fresh database connections; previously committed pages can be replayed without duplicating evidence or energy.
6. **Transaction identity:** current projections are keyed by `(source_id, charger_id, transaction_id)`, so equal transaction IDs at different charge points do not collide.

## Explicit non-goals

No synthetic StopTransaction frames, inferred completion, automatic source-reset recovery, direct access to CSMS SQLite, second transaction cursor, or multi-source legacy warehouse migration. Historical adapters remain tracked separately in issue #18.
