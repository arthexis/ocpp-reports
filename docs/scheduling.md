# B5: unified one-shot run with external systemd timer

`ocpp-reports run --source verified-summary.json` validates a complete internal summary and prints a plain-text report. Use `--output report.txt` to write a new file, or `--send` to queue and attempt SMTP delivery to the configured operator. With no output or send, the report is printed. `--send` is opt-in.

Period selection: `--period previous-day|previous-week|previous-month` (default previous-day), or explicit `--since ISO --until ISO` with timezone offsets. `--timezone America/Monterrey` selects local calendar boundaries for previous periods. The source's half-open interval must exactly match the requested interval. The source must already exist as a verified `ocpp-reports/summary/v1` document; B2 export integration remains separate.

The systemd service uses the same `run` command, with `--send`. The daily timer uses `Persistent=true`, which triggers once after downtime, **not** once per missed period. The sample service expects a prepared summary at `/var/lib/ocpp-reports/summary.json`; deployment must arrange that input and SMTP environment securely.

**Do not enable the timer yet.** B5.3 durable period reservations are implemented, but the export-to-summary producer (B2) and deployment validation remain outstanding. No embedded scheduler or customer email delivery.

## B5.3 recovery semantics

A period reservation and its delivery are committed in one SQLite transaction. Repeated `run --send` invocations for the same source, operator, and UTC boundaries report an existing delivery and do not send again. The reservation persists across restarts. An interrupted SMTP attempt remains `sending`; a failed attempt remains `failed`. Neither is retried automatically. Inspect `ocpp-reports mail list`, check the operator inbox, and use `ocpp-reports mail retry KEY` then `ocpp-reports mail send` deliberately if necessary. SMTP acceptance is not proof of inbox delivery.

B5.4 still needs an end-to-end deployment review, including report input freshness, SMTP configuration, and systemd timer safety.

## B5.4 deployment review

The systemd unit stores its SQLite outbox at `/var/lib/ocpp-reports/outbox.sqlite3` using `StateDirectory=ocpp-reports`. Set `OCPP_REPORTS_OPERATOR_EMAIL`, `OCPP_REPORTS_SMTP_HOST`, `OCPP_REPORTS_SMTP_FROM`, and any SMTP TLS/port/login settings in a root-protected `/etc/ocpp-reports/ocpp-reports.env` file. The service must be installed at the actual executable path; `/usr/bin/ocpp-reports` is illustrative until verified on the host. The `ocpp-reports` service user and state permissions must be provisioned before enabling.

The timer's `OnCalendar` follows the system manager timezone, while `--timezone America/Monterrey` sets the report calendar. Align these explicitly when deploying. `Persistent=true` performs only one catch-up activation after downtime. The summary file must be refreshed atomically by a trusted producer before the timer fires; the current service does not generate it. A stale or wrong-period summary is refused, but a same-period stale revision cannot be detected without B2/CSMS producer metadata. This is a deployment gate, not a reason to silently send outdated reports.

A scheduled `run --send` now claims only its own delivery key; unrelated pending emails remain untouched. Manual recovery remains `mail retry KEY` followed by `mail send --key KEY`.
