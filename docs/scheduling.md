# B5: unified one-shot run with external systemd timer

`ocpp-reports run --source verified-summary.json` validates a complete internal summary and prints a plain-text report. Use `--output report.txt` to write a new file, or `--send` to queue and attempt SMTP delivery to the configured operator. With no output or send, the report is printed. `--send` is opt-in.

Period selection: `--period previous-day|previous-week|previous-month` (default previous-day), or explicit `--since ISO --until ISO` with timezone offsets. `--timezone America/Monterrey` selects local calendar boundaries for previous periods. The source's half-open interval must exactly match the requested interval. The source must already exist as a verified `ocpp-reports/summary/v1` document; B2 export integration remains separate.

The systemd service uses the same `run` command, with `--send`. The daily timer uses `Persistent=true`, which triggers once after downtime, **not** once per missed period. The sample service expects a prepared summary at `/var/lib/ocpp-reports/summary.json`; deployment must arrange that input and SMTP environment securely.

**Do not enable the timer yet.** B5.3 must implement durable per-period state and restart/replay protection, and B5.4 must verify full integration and deployment. No embedded scheduler or customer email delivery.

## B5.3 recovery semantics

A period reservation and its delivery are committed in one SQLite transaction. Repeated `run --send` invocations for the same source, operator, and UTC boundaries report an existing delivery and do not send again. The reservation persists across restarts. An interrupted SMTP attempt remains `sending`; a failed attempt remains `failed`. Neither is retried automatically. Inspect `ocpp-reports mail list`, check the operator inbox, and use `ocpp-reports mail retry KEY` then `ocpp-reports mail send` deliberately if necessary. SMTP acceptance is not proof of inbox delivery.

B5.4 still needs an end-to-end deployment review, including report input freshness, SMTP configuration, and systemd timer safety.
