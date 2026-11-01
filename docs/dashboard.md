# M2 standalone dashboard

Run `ocpp-reports ingest --follow --data-dir /path/to/csms/schemas/8 --database /var/lib/ocpp-reports/reports.sqlite` separately, then:

```sh
ocpp-reports serve --database /var/lib/ocpp-reports/reports.sqlite --host 127.0.0.1 --port 8080
```

Open http://127.0.0.1:8080/ locally. The dashboard reads **only** Reports-owned SQLite, using a read-only connection. It does not import CSMS Python modules or contact chargers. It displays last-known state with an observation age warning after 30 seconds, plus latest transactions, events and source cursors. Filter by source or charger. A JSON read-only endpoint is available at `/api/snapshot`.

Security: `serve` rejects non-loopback binds. It has no user accounts or authentication; do not expose it publicly. M4 will add nginx HTTPS, authentication, and Ansible services. The browser refreshes every 15 seconds; no WebSockets. M2 does not require forwarder or collector.

**Field validation:** install on customer vendor dedicated host; verify warehouse is populated, dashboard shows live/stale connector state, active and stopped transactions, and survives stopped ingest with visibly old timestamps. This PR does not claim that field validation has occurred.

## Customer source boundaries

Run one loopback process per customer against the same collector warehouse:

```sh
ocpp-reports serve --database /var/lib/ocpp-reports/warehouse.sqlite --port 8080 \
  --title 'Porsche OCPP Reports' --source-id gway-005 --source-id porsche-abb-1
ocpp-reports serve --database /var/lib/ocpp-reports/warehouse.sqlite --port 8082 \
  --title 'Audi OCPP Reports' --source-id gway-002
```

Repeated `--source-id` arguments are an enforced allowlist for the HTML page,
JSON API, local records, replicated records and source cursors. URL filters can
only narrow that scope; another source returns 403. Without these arguments,
the backwards-compatible operator dashboard shows all sources. Bind only to
loopback and use independent HTTPS virtual hosts and password files.

Provision `/etc/ocpp-reports/dashboards.json` (root-owned, mode 0640) to make
these instances persistent across Watchtower/control deployment:

```json
[
  {"name": "dashboard", "title": "Porsche OCPP Reports",
   "hostname": "porsche.gelectriic.com", "port": 8080,
   "source_ids": ["gway-005", "porsche-abb-1"],
   "htpasswd": "/etc/ocpp-reports/dashboard.htpasswd"},
  {"name": "dashboard-audi", "title": "Audi OCPP Reports",
   "hostname": "audi.gelectriic.com", "port": 8082,
   "source_ids": ["gway-002"],
   "htpasswd": "/etc/ocpp-reports/dashboard-audi.htpasswd"}
]
```

Provision certificates at `/etc/letsencrypt/live/<hostname>/` and each password
file first. The dashboard playbook does not generate credentials or certificates.
Existing base-unit regeneration cannot remove the source restrictions because
these are persistent systemd overrides. Apply only dashboard services/proxies:

```sh
sudo ansible-playbook -i localhost, ansible/dashboard-instances.yml
```

The playbook is also included in Watchtower/control deployments when the inventory
exists. Adding a new instance does not change collector credentials or source
identities. Removing an inventory entry does not delete an existing instance;
explicitly disable its service and Nginx site when retiring it. Keep the persistent
ACME webroot `/var/www/ocpp-reports-dashboard-acme` available for renewal.

## Dashboard users

Nginx HTTP Basic authentication provides per-dashboard user/password access;
there are no application accounts, roles or self-service signup. Anyone in a
dashboard's password file can view all sources allowed by that dashboard.
A username can exist in both files with independent passwords. Collector bearer
credentials are separate and must never be used as dashboard passwords.

On the customer host, add a user or change their password interactively:

```sh
sudo htpasswd -B /etc/ocpp-reports/dashboard-audi.htpasswd USERNAME
sudo htpasswd -B /etc/ocpp-reports/dashboard.htpasswd USERNAME
```

Do not use `-c` on existing files (it erases other users), or `-b` (it puts the
password in process arguments). For initial file creation use `sudo install -o
root -g www-data -m 0640 /dev/null PATH`, then `htpasswd -B PATH USERNAME`.
Remove access with `sudo htpasswd -D PATH USERNAME`; list only usernames with
`sudo cut -d: -f1 PATH`. Nginx reads the file on requests; no reload is needed.
Browsers cache Basic credentials, so test with a private window after changes.


## Event groups: read-only window implementation (PR #127)

The **Eventos recientes** panel now selects the latest **10 groups per page**
with SQLite window functions over the same source-scoped local and replicated
events. Only adjacent routine Heartbeats and eligible benign status messages
collapse; faults and transaction lifecycle events remain individual groups.
Grouping follows the existing source/event-ID ordering rather than merging
unrelated events across sources. SQL selects summaries only, so opening the
dashboard does not download the groups' underlying event payloads.

Expanding a group fetches up to **50 display-only event entries** using
`GET /api/event-group?group_source=SOURCE&first_event_id=ID&offset=0`.
The API revalidates the exact group boundary and source allowlist on each
request. The browser can request the next 50 entries explicitly. The JSON
response intentionally contains only event ID, source, charger, location,
action, and timestamp fields, not raw OCPP payloads, RFID or customer secrets.
Event-group queries never write to the warehouse, CSMS or replication cursors.

**Field performance check:** capture dashboard response header
`Server-Timing: event-groups;dur=...` and the scoped group-detail JSON
`query_duration_ms` on several normal refreshes and expanded large groups.
Compare warm/cold timings to current dashboard response latency and watch
CPU/SQLite load; treat repeated high latency as evidence for a future
materialized Reports-owned group projection. Avoid creating derived tables or
indexes in the authoritative CSMS database. A 10,000-event synthetic
heartbeat group is exercised in tests, but synthetic timing is not a
production performance guarantee.
