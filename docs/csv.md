# B3.1 deterministic CSV renderer

`render_report(summary, "csv")` produces UTF-8-compatible CSV text from the established `ocpp-reports/summary/v1` document. It does not read CSMS exports or depend on B2.

Columns (v1, fixed order): `id,charger_id,connector_id,state,active,rfid,started_at,stopped_at,energy_wh,completeness`. The field names follow existing B3 summary fixtures; unavailable optional fields are empty, not inferred. Energy remains in Wh in this machine-oriented format, with no fabricated conversion. Each transaction has one row; empty reports contain a header only. Sorting is by charge point and transaction identity, and duplicate identities fail explicitly. Completeness is `complete` only for completed transactions with recorded energy; other rows are `provisional`.

Untrusted text beginning with spreadsheet formula operators (`=`, `+`, `-`, `@`, including after leading spaces) is prefixed with a single quote to reduce spreadsheet formula execution risk. Numeric measurements remain numeric. Standard Python CSV quoting handles Unicode, commas, quotes and newlines.

`generate --format csv` and `run --format csv` now use the same validated CSV renderer and fixed columns. `generate` reads CSMS export pages; `run` reads a verified internal summary. `run` defaults to text and supports CSV as manual output only; `--send` with CSV is explicitly rejected. HTML remains available through the rendering API, not the `run` CLI. No changes to SMTP or scheduling are included here.
