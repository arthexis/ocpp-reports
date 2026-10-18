# H0 — Offline historical-source inventory

Status: **repository reconnaissance complete; physical artifact validation pending**. This document describes source-code evidence only. No live databases, backups, or genuine historical rows were available in the GitHub repositories investigated. Never treat model declarations or invented fixtures as proof of deployed data.

## Source inventory

| Source family | Code evidence | Candidate offline input | Confirmed deployed sample | Primary blockers |
| --- | --- | --- | --- | --- |
| Current standalone CSMS | `arthexis/ocpp-csms/src/ocpp_csms/schema.py`, `export_contract.py` | Supported `ocpp-csms/export/v1` page | Reports contract fixtures only | No historical-source blocker; existing importer |
| Old standalone CSMS | CSMS schema migration definitions for versions 1–8 and `schema_storage.py` | Historical `ocpp-csms.sqlite3` and transaction JSON archives | **None** | Exact deployed versions, schema signature, locations and samples |
| Newer Arthexis Django | `arthexis/arthexis/apps/ocpp/models/sessions.py`, `assets.py`, migrations `0001`–`0027` | Offline SQLite snapshot or offline PostgreSQL backup, depending on deployment | **None** | DB engine, migration level, authority-cutover overlap, physical rows |
| Arthexis Legacy Django | `arthexis/arthexis-legacy/apps/ocpp/models/transaction.py`, `meter_value.py`, `transactions_io.py`, `config/settings/database.py` | Existing transaction JSON export, SQLite snapshot or PostgreSQL backup | **None** | Stable native IDs in JSON, energy unit conventions, backup format |
| `ocpp_gelectriic` | Server log writer; no confirmed database schema | Rotated OCPP logs, if retained | **None** | Unknown completeness/identities; not yet an adapter candidate |
| `ocpp-asgi`, `ocpp` | Transport and protocol library | None identified | **None** | No independently identified historical store |

## Observed source signatures (from code, not backups)

### Standalone CSMS

- SQLite database name `ocpp-csms.sqlite3`; schema evolution through `PRAGMA user_version` 1–8.
- `events` contains ordered event IDs, OCPP actions and JSON payload; later schemas add `transactions`, `connector_status`, RFID data, appliance metadata, start identity/allocator and transaction views.
- Version 8 removes retired transaction-recovery storage; signatures **must be checked against actual table and column metadata**, not solely `user_version`.
- Historical versions **cannot be started**; current `export/v1` is only for current CSMS.

### Newer Arthexis Django

- `OcppTransaction` has charger FK, nullable connector FK, unique `remote_id`, `started_at`, nullable `stopped_at`, `energy_kwh`, `historical`, `recovery_state` and meter derivation revisions.
- `MeterValue` stores timestamp, decimal value, unit, multiplier and source fingerprint; `MeterReadingBatch` can hold unbound evidence.
- Migration `0017_authority_cutover_historical_transaction.py` introduces charger `authority_cutover_at` and transaction `historical`; check these when comparing overlaps.

### Arthexis Legacy Django

- `Transaction` has string `ocpp_transaction_id`, charger FK, `start_time`, nullable `stop_time`, receipt times and integer meter boundaries.
- `MeterValue` contains `timestamp` and nullable decimal `energy`; charger metadata has configurable `energy_unit`.
- `transactions_io.export_transactions` describes existing JSON with `chargers` and `transactions` arrays, including nested `meter_values`, but omits native Django transaction PK; do not assume this export is uniquely keyed.
- `config/settings/database.py` supports both SQLite and PostgreSQL. No old application code may be executed.

## Safe offline acquisition checklist

For each historical source, obtain an authorized **copy** of a backup or export. Record the source appliance/deployment identity, approximate period, artifact name, checksum, size, acquisition timestamp, and backup format. Avoid RFID, account/customer data, credentials, access tokens and raw sensitive payloads in shared fixtures.

For SQLite copies, inspect using a standalone SQLite reader with `mode=ro` and `PRAGMA query_only=ON`. Record `PRAGMA user_version`, `sqlite_master` table/view names, `PRAGMA table_info` for candidate transaction/event/meter tables, `django_migrations` if present, table counts and min/max timestamps where columns exist. Do **not** invoke application migrations, create schema objects, run `VACUUM`, or open the original database for writes. For active WAL-mode sources, acquire a consistent snapshot first.

For PostgreSQL backups, identify whether the artifact is plain SQL, custom-format `pg_dump`, directory or tar archive; record dump metadata without starting Arthexis. A verified isolated restore may be required for row-level extraction later; never restore into or connect to production as a default. Preserve the backup unmodified.

For JSON exports, validate root structure and inspect a sanitized sample with native IDs, time fields, units and nullable energy. Do not invent stable transaction keys for files that omit them.

## Evidence still needed to close H0

- [ ] Actual historical CSMS SQLite/JSON artifact(s), version signatures, counts, date ranges and sanitized representative records.
- [ ] Actual newer Arthexis offline backup or export, database backend, `django_migrations` and sample transaction/meter evidence.
- [ ] Actual Arthexis Legacy offline backup or preexisting export, database backend, native key and energy unit audit.
- [ ] Source-level charger identity mapping, time ranges and any cutover/overlap between generations.
- [ ] PostgreSQL backup-format feasibility if PostgreSQL was used.

**No historical application may be started** to satisfy this checklist. Absence of an artifact means the corresponding source is **unverified**, not empty.

## H0 exit criterion

Close H0 only when at least one genuine, authorized historical CSMS artifact and one genuine Arthexis-family artifact have been inventoried and sampled safely, or the user explicitly accepts a reduced source scope. Until then, proceed with architecture discussion only; adapter mappings are provisional.
