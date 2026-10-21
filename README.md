# OCPP Reports

Independent, offline-first, read-only reporting for OCPP CSMS.

Development tracked in [issue #1](https://github.com/arthexis/ocpp-reports/issues/1). All implementation changes are proposed through pull requests.

## Operator CLI

The single executable is `ocpp-reports`; there is no singular `ocpp-report` alias. Invoking it without a subcommand displays help. The operations have distinct responsibilities:

- `ocpp-reports status` reports application status.
- `ocpp-reports ingest --source page.json --database reports.sqlite3` validates and atomically persists **one** local CSMS `export/v1` page. The command is explicit and does not alter its source. Import sequential pages in cursor order; `--json` prints the resulting cursor.
- `ocpp-reports run --source reports.sqlite3 --source-id SOURCE --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z --output report.json` generates from the Reports-owned warehouse. It can also consume a CSMS export JSON file or directory directly, without ingestion; `--format csv` selects CSV.
- `ocpp-reports run --source verified-summary.json --period previous-day` renders a verified summary. `run` also accepts a CSMS export file/directory or the Reports SQLite warehouse. `--format json|csv|text` controls output; `--send` explicitly enables text delivery. No implicit ingestion or network query occurs.

These are one-shot operations. A direct export snapshot is not a live CSMS status query. No background scheduler or automatic ingest is started by the CLI.

### Audi CSV compatibility profile

`ocpp-reports run --source reports.sqlite3 --source-id SOURCE --since 2026-01-01T00:00:00Z --until 2026-02-01T00:00:00Z --format csv --profile audi --locations locations.json --output audi.csv`

The optional `locations.json` is a JSON object mapping charge-point IDs to agency names, for example `{"CP-001":"Calzada","CP-002":"Vasconcelos"}`. The profile reproduces the ten historical columns `LOCACION,CONECTOR,FECHA INICIO,FECHA FINAL,WH INICIO,WH FINAL,WH USADOS,% INICIAL,% FINAL,RAZON FINAL`. The legacy `ARCHIVO FUENTE` column is deliberately omitted. Unavailable values are blank, including missing location, SoC and stop reason; zero energy is preserved as zero. Dates are formatted in the requested report timezone. The existing standard CSV remains unchanged. The CSMS export currently supplies start/stop meter readings but does not export stop reason or SoC in its transaction snapshots; see `arthexis/ocpp-csms#270`.

### Automatic local reporting

When `--source` is omitted, `run` discovers the running local `ocpp-csms.service`, synchronizes its schema-8 evidence into the Reports-owned warehouse (default `/var/lib/ocpp-reports/reports.sqlite3`, override with `OCPP_REPORTS_DATABASE`), and then renders the requested period. The service must expose an explicit `--data-dir` argument and the operator needs read access to the CSMS evidence and write access to the warehouse. Failed synchronization refuses the report; explicit `--source` remains fully offline. For example:

```sh
ocpp-reports run --period previous-month --timezone America/Monterrey --format csv --output report.csv
```

### Run examples

```sh
# Direct CSMS export, without ingesting
ocpp-reports run --source export.json --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z --format json

# Reports-owned warehouse; select a source and write CSV
ocpp-reports run --source reports.sqlite3 --source-id SOURCE --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z --format csv --output report.csv

# Verified summary for the previous completed day; send only on request
ocpp-reports run --source verified-summary.json --period previous-day --send
```

`--since` and `--until` are a half-open, offset-aware interval and must be supplied together. `--period` supports `previous-day`, `previous-week`, `previous-month`, and `current-month` (month-to-date, ending at execution time) when explicit bounds are omitted. Output files are create-only; the source is never overwritten. `--send` requires text output, a complete report, and configured mail delivery. CSV/JSON output never sends email. `check` remains available.

### Reporting timezone

`run` uses `UTC` by default, or `America/Monterrey` when `--profile audi` is selected. Pass `--timezone IANA_ZONE` to override either default. The selected zone determines previous-day/week/month boundaries and how the Audi CSV timestamps are displayed. Explicit `--since` and `--until` retain their supplied offsets. Previously verified summaries retain their own recorded reporting period and timezone; changing the zone requires generating a new summary from source data.
