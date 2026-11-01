# OCPP Reports

Independent, offline-first, read-only reporting for OCPP CSMS.

Development tracked in [issue #1](https://github.com/arthexis/ocpp-reports/issues/1). All implementation changes are proposed through pull requests.

## Local preflight before opening a PR

Use one non-deploying command from a working checkout to catch template, syntax,
and Python regressions before using GitHub Actions as the first test environment:

```sh
python3 -m pip install -e '.[test]' ansible-core ansible-lint
bash scripts/preflight.sh              # full: unit tests + shell + Ansible + rendered service units
bash scripts/preflight.sh --quick      # fast static/artifact checks, no full test suite
```

Both modes render the dashboard systemd unit using Ansible's Jinja
`trim_blocks` setting, assert the `ExecStart` and `Restart` directives remain
separate, and invoke `systemd-analyze verify` when available. Neither mode
installs services, contacts running collectors, or modifies evidence databases.
CI runs the quick preflight first; Python tests and the four isolated deployment
roles only start after it passes. Isolated deployment failures automatically
print the generated dashboard unit, service state, and recent journal.
The full cross-repository contract test in CI additionally installs the
`ocpp-csms` producer from `main`; operators can do the same locally if needed.

## Operator CLI

The single executable is `ocpp-reports`; there is no singular `ocpp-report` alias. Invoking it without a subcommand displays help. The operations have distinct responsibilities:

- `ocpp-reports status` reports application status.
- `ocpp-reports collector status|hold|reset|release --source SOURCE --database collector.sqlite` provides repeatable source maintenance. Reset requires a hold and `--backup NEW_PRIVATE_PATH`, clears only that source, and leaves it held. Follow the [coordinated reset and rollback procedure](docs/collector-maintenance.md).
- `ocpp-reports metadata dates` previews or repairs selected completed transaction dates from preserved charger event timestamps. Apply requires a private SQLite backup; see the [historical transaction date procedure](docs/historical-transaction-dates.md).
- `ocpp-reports ingest --source page.json --database reports.sqlite3` validates and atomically persists **one** local CSMS `export/v1` page. The command is explicit and does not alter its source. Import sequential pages in cursor order; `--json` prints the resulting cursor.
- `ocpp-reports run --source reports.sqlite3 --source-id SOURCE --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z --output report.json` generates from the Reports-owned warehouse. It can also consume a CSMS export JSON file or directory directly, without ingestion; `--format csv` selects CSV.
- `ocpp-reports run --source verified-summary.json --period previous-day` renders a verified summary. `run` also accepts a CSMS export file/directory or the Reports SQLite warehouse. `--format json|csv|text` controls output; `--send` explicitly enables text delivery. No implicit ingestion or network query occurs.

These are one-shot operations. A direct export snapshot is not a live CSMS status query. No background scheduler or automatic ingest is started by the CLI.

### vendor CSV compatibility profile

`ocpp-reports run --source reports.sqlite3 --source-id SOURCE --since 2026-01-01T00:00:00Z --until 2026-02-01T00:00:00Z --format csv --profile vendor --locations locations.json --output vendor.csv`

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

`run` uses `UTC` by default, or `America/Monterrey` when `--profile vendor` is selected. Pass `--timezone IANA_ZONE` to override either default. The selected zone determines previous-day/week/month boundaries and how the vendor CSV timestamps are displayed. Explicit `--since` and `--until` retain their supplied offsets. Previously verified summaries retain their own recorded reporting period and timezone; changing the zone requires generating a new summary from source data.


## Watchtower HTTPS setup (customer)

Watchtower and control installations enable Dashboard, Collector and Diagnostics HTTPS by default. Satellite and terminal installations do not publish these services. To explicitly disable watchtower HTTPS, set `OCPP_REPORTS_NGINX_ENABLED=false` for deployment.

The installer installs `nginx`, `certbot`, `python3-certbot-nginx`, `acl`, and `apache2-utils` (provides `htpasswd`) when publishing HTTPS. It does not invent or rotate passwords, replication tokens, or Let's Encrypt accounts.

Before the first public exposure, ensure DNS points to the server and issue certificates for the two independent names:

```sh
sudo certbot certonly --nginx -d collector.example.com
sudo certbot certonly --nginx -d customer.example.com
sudo certbot certificates
```

The diagnostic hostname uses its independent certificate, managed by the diagnostics deployment. If the Nginx Certbot plugin is missing on an older installation, install `python3-certbot-nginx` first.

Provision the Dashboard login once (interactive password prompt; do not put the password on the command line):

```sh
sudo htpasswd -c /etc/ocpp-reports/dashboard.htpasswd admin
sudo chown root:www-data /etc/ocpp-reports/dashboard.htpasswd
sudo chmod 0640 /etc/ocpp-reports/dashboard.htpasswd
```

Only use `-c` when creating the file. For subsequent users, omit it to preserve existing credentials. Also provision the Collector token mapping in `/etc/ocpp-reports/collector.toml`, with a matching token in each satellite's `/etc/ocpp-reports/forwarder.toml`; keep these files protected.

Deploy and verify on the watchtower:

```sh
./deploy.sh --watchtower
sudo nginx -t
curl -i https://collector.example.com/api/replication
curl -I https://customer.example.com/
```

For an unauthenticated GET, Collector should return **403** (Nginx allows only POST to `/api/replication`) and Dashboard should return **401** with the Basic Auth challenge. Both must pass TLS hostname validation without `curl -k`. These responses verify HTTPS routing and access control, *not* successful replication; on the satellite, verify `forward_cursors` advances and compare the Collector's acknowledged source cursor.

If deployment rejects HTTPS prerequisites, look for the exact missing path in Ansible output. Required files include the fullchain and private key for both names, the diagnostic certificate, `/etc/ocpp-reports/dashboard.htpasswd`, and `/etc/ocpp-reports/collector.toml`. Do not disable TLS verification to bypass a missing certificate.
