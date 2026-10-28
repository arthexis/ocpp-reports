# M2 standalone dashboard

Run `ocpp-reports ingest --follow --data-dir /path/to/csms/schemas/8 --database /var/lib/ocpp-reports/reports.sqlite` separately, then:

```sh
ocpp-reports serve --database /var/lib/ocpp-reports/reports.sqlite --host 127.0.0.1 --port 8080
```

Open http://127.0.0.1:8080/ locally. The dashboard reads **only** Reports-owned SQLite, using a read-only connection. It does not import CSMS Python modules or contact chargers. It displays last-known state with an observation age warning after 30 seconds, plus latest transactions, events and source cursors. Filter by source or charger. A JSON read-only endpoint is available at `/api/snapshot`.

Security: `serve` rejects non-loopback binds. It has no user accounts or authentication; do not expose it publicly. M4 will add nginx HTTPS, authentication, and Ansible services. The browser refreshes every 15 seconds; no WebSockets. M2 does not require forwarder or collector.

**Field validation:** install on customer vendor dedicated host; verify warehouse is populated, dashboard shows live/stale connector state, active and stopped transactions, and survives stopped ingest with visibly old timestamps. This PR does not claim that field validation has occurred.
