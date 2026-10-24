# Porsche Watchtower + gway-005 satellite validation

This deployment joins the Watchtower's locally managed ABB charger and the
e-tron charger managed by gway-005 in one **read-only dashboard**. The two
CSMS instances stay independent. Collector traffic uses
`https://collector.gelectriic.com/api/replication`; dashboard traffic uses
`https://porsche.gelectriic.com`.

## Prepare

1. Verify both hosts have current `ocpp-csms.service`, preserve existing
   Reports role markers, and deploy Reports with their already selected roles
   (`watchtower` for the Porsche server, `satellite` for gway-005).
   Do not uninstall to change a role during this test.
2. On the Porsche host, configure `/etc/ocpp-reports/collector.toml`
   via `ocpp-reports collector config` with a distinct token for `gway-005`:
   `[sources]` followed by `gway-005 = "<secret>"`.
3. Enable Watchtower local ingestion using
   `OCPP_REPORTS_LOCAL_INGEST_ENABLED=true` at deployment if not already
   enabled. Check its active CSMS schema data dir (default schema 8).
4. On gway-005, edit `/etc/ocpp-reports/forwarder.toml` using
   `ocpp-reports forwarder config`; set
   `source_id = "gway-005"`, `endpoint =
   "https://collector.gelectriic.com/api/replication"`, and the same token.
   Keep protected TOML files mode 0600 or 0640 as required by Ansible.
5. On the Porsche host ensure the collector TLS certificate, dashboard TLS
   certificate, dashboard HTTP Basic credentials and nginx vhosts exist.
   Enable full Reports nginx exposure only after verifying HTTPS prerequisites.
   Do not modify the CSMS WebSocket hostname or exposure.

## Acceptance

- `systemctl is-active ocpp-reports-ingest.service` on both hosts.
- `systemctl is-active ocpp-reports-forwarder.service` on gway-005.
- `systemctl is-active ocpp-reports-collector.service` and
  `systemctl is-active ocpp-reports-dashboard.service` on Porsche.
- `ocpp-reports forwarder config --check` and
  `ocpp-reports collector config --check` succeed on their respective hosts.
- Authenticated `GET /api/snapshot` on the dashboard includes distinct
  source IDs for the local ABB and remote e-tron. Filtering by source and
  charger works; an interrupted forwarder does not remove past evidence.
- Collector rejects unauthenticated replication requests; no token appears
  in logs or CI output; repeated batches do not duplicate records.
- Record the replication cursor and last observations; ensure no data
  modifications or downtime in either CSMS.

**Scope note:** this increment unifies the dashboard snapshot (connector
state, recent transactions and events). Legacy `run/generate` reporting
still reads the local ingestion transaction projection; consolidated
historical CSV/email aggregation needs a separate contract and validation.
