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


## Host evidence update (2026-10-18)

The field-host [inventory comment](https://github.com/arthexis/ocpp-reports/issues/18#issuecomment-6246266793) identifies actual CSMS schema-2 (8,955 events), schema-3 (15,404 events), and four older Arthexis SQLite snapshots (317, 14, 38 and 229 transactions respectively). This **supersedes** the earlier statement that no deployed historical artifacts were found. The physical backups are **not in GitHub**, and their contents have not been independently inspected by this PR.

CSMS schema-2 is contained in schema-3, which is contained in schema-6 by matching event ID/payload: these are nested snapshots, not additive transaction sources. The older Arthexis generations include missing OCPP IDs, missing stops, missing meter readings, naive timestamps and unverified energy units. No newer Arthexis backend snapshot is known.

### H0a metadata collection on the host

Copy or otherwise acquire a consistent, quiescent backup **before** inspection. Never point the utility at a live SQLite/WAL database: immutable reads deliberately ignore WAL and could miss committed data. The tool does not start any historical service, import its code or extract raw rows.

```sh
python3 scripts/h0_inventory.py \\
  /home/arthe/ocpp-csms-data/ocpp-csms.sqlite3.schema-2.bak \\
  /home/arthe/ocpp-csms-data/ocpp-csms.sqlite3.schema-3.bak \\
  /home/arthe/arthexis.bkp/db.sqlite3 \\
  /home/arthe/arthexis.bkp2/db.sqlite3 \\
  /home/arthe/arthexis.bkp3/db.sqlite3 \\
  /home/arthe/arthexis/db.sqlite3 \\
  --output h0-inventory.json
```

The output is a create-only JSON manifest with source filename, size, SHA-256, SQLite schema version, integrity result, selected table names/columns/counts and available time bounds. It deliberately omits row values, IDs, payloads and meter readings. **Column names and filenames may still reveal implementation details; review the manifest before sharing.** Hashes identify artifacts and are not anonymization. Do not commit backups, full dumps or raw transaction/RFID data.

### Remaining H0b gate

- [ ] Run the inventory tool on authorized offline copies and review the manifest.
- [ ] Produce separate, deliberately sanitized fixture records with stable pseudonymous join keys (this metadata tool intentionally does **not** do row extraction).
- [ ] Establish actual timezone and meter-energy unit semantics from historical producer code and source evidence.
- [ ] Resolve source lineage/identity, native transaction key fallback and observed overlap before freezing adapter mappings.


### H0b bounded sanitized fixtures (code ready; field evidence pending)

`scripts/h0_fixtures.py` reads a **quiescent offline SQLite backup** with `mode=ro&immutable=1` and exports at most five records per recognized table by default. Only explicit, audited field names are eligible: timestamps, limited numeric meter/energy fields, a few known action/unit enums and **salted pseudonyms** for local row IDs. It does **not** export RFID, raw OCPP JSON payloads, charger IDs, customer names, or arbitrary strings. The salt must remain private and must be shared across runs only when cross-snapshot pseudonym stability is desired. Pseudonymized IDs alone do not prove cross-database identity.

```sh
# On Gway-001, only AFTER the field-test readiness gate is met:
export H0_FIXTURE_SALT="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
python3 scripts/h0_fixtures.py /path/to/quiescent-copy.sqlite3 \\
  --output /path/outside/backups/h0-fixtures.json --limit 5
unset H0_FIXTURE_SALT
```

Review the generated output manually before sharing or committing. The file is created exclusively (`x` mode), so existing outputs cannot be overwritten. The tool is **not** a production data export, does not resolve timezone or energy units, and does not prove overlap; these are H0b validation tasks. Fixture schema is `h0-sanitized-fixtures/v1`. CI tests use synthetic records only.

**H0b remaining work:** inspect actual metadata manifest, verify column mappings and historical producer units, determine naive timestamp interpretation, assess source lineage and create an agent acceptance script with expected counts. Do not claim H0 complete until that evidence is available.


### H0b semantics gate and Gway-001 handoff

`scripts/h0_readiness.py` compares an H0 inventory, sanitized fixtures and a **manually reviewed** source policy. It checks artifact hashes, SQLite schema versions, sample-table existence/counts, source signatures, explicit lineage, timezone, energy units and evidence citations. A failed or unknown assertion exits with code 2. This is **not** proof of the declared assertions: an operator must review the actual evidence and policy; arbitrary self-declarations are not validation.

Example policy (replace SHA and assertions **only after verifying actual historical producer behavior**):

```json
{
  "sources": {
    "REPLACE_WITH_REAL_SHA256": {
      "lineage_id": "REVIEWED_APPLIANCE_LINEAGE",
      "timezone": "UTC",
      "energy_unit": "kWh",
      "evidence": "REPLACE_WITH_REVIEWED_CODE_OR_FIELD_EVIDENCE_REFERENCE"
    }
  }
}
```

The above is illustrative and **must not** be used as an accepted policy. For source generations with no energy fields, use `not_applicable` only when justified by actual schema. Never infer a timezone from a naive timestamp or assume an Arthexis numeric value is kWh. CSMS v2/v3 event snapshots share lineage but the inventory alone cannot deduplicate their records. Future adapters must compare native event identity/payload hashes; do not sum snapshot counts.

**Agent handoff once readiness conditions are met:** run metadata inventory on all six quiescent copies; run fixture extractor for each with a private salt and distinct output path; review all outputs for sensitive content; prepare one policy entry per artifact SHA with cited evidence; run:

```sh
python3 scripts/h0_readiness.py --inventory h0-inventory.json \\
  --policy h0-policy.json --fixtures h0-fixture-*.json \\
  --output h0-readiness.json
```

Expected: code 0 and `ready: true` only when every policy assertion and fixture-to-artifact check passes. Code 2 means field acceptance is **blocked**; preserve the diagnostics and resolve uncertainties, rather than weakening the checks. Compare original SHA-256 before/after and ensure no new SQLite journals or writes. No backup, salt, raw identifiers or unreviewed fixtures should be uploaded to GitHub.

**Current gate remains closed:** the real field manifests, sanitized fixtures, verified timezone/energy semantics and lineage evidence are not yet available in this repository. These checks are preparatory, not a claim that H0b has been field-validated.
