# OCPP Reports

Independent, offline-first, read-only reporting for OCPP CSMS.

Development tracked in [issue #1](https://github.com/arthexis/ocpp-reports/issues/1). All implementation changes are proposed through pull requests.

## Operator CLI

The single executable is `ocpp-reports`; there is no singular `ocpp-report` alias. Invoking it without a subcommand displays help. The operations have distinct responsibilities:

- `ocpp-reports status` reports application status.
- `ocpp-reports ingest --source page.json --database reports.sqlite3` validates and atomically persists **one** local CSMS `export/v1` page. The command is explicit and does not alter its source. Import sequential pages in cursor order; `--json` prints the resulting cursor.
- `ocpp-reports generate --source reports.sqlite3 --source-id SOURCE --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z --output report.json` generates from the Reports-owned warehouse. It can also consume a CSMS export JSON file or directory directly, without ingestion; `--format csv` selects CSV.
- `ocpp-reports run --source verified-summary.json --period previous-day` renders an already verified summary for the operator. `--send` explicitly enables the configured delivery workflow; `run` does **not** implicitly ingest or query CSMS. Integration of `run` with direct generation and named jobs is a separate follow-up.

These are one-shot operations. A direct export snapshot is not a live CSMS status query. No background scheduler or automatic ingest is started by the CLI.
