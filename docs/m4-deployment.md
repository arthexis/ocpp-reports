# M4 deployment profiles

The role is selected on the first install and cannot change without uninstalling.
`./deploy.sh` defaults to `--terminal` on first install, and uses the
persisted role thereafter. Repeating the same explicit role is safe.

| Role | Services |
| --- | --- |
| terminal | CLI reporting and mail (no network services) |
| satellite | Forwarder |
| watchtower | Collector and dashboard |
| control | Forwarder, Collector and dashboard |

The collector and dashboard bind to loopback only. Do **not** expose ports
8080/8081 directly to the Internet. A TLS reverse proxy with authentication
is required for remote dashboard access.

## Forwarder configuration

Create `/etc/ocpp-reports/forwarder.env` with the following fields:

```ini
REPORTS_SOURCE_ID=site-001
REPORTS_COLLECTOR_URL=https://reports.example.org/api/replication
```

Create `/etc/ocpp-reports/forwarder-token` containing the source-specific
bearer token. Both files must be readable by the `ocpp-reports` service
identity; use root ownership, group `ocpp`, and mode `0640`.
Avoid committing either file. Run `./deploy.sh` again after configuration.

## Collector configuration

Create `/etc/ocpp-reports/collector-credentials.json` containing a JSON
object mapping authorized source IDs to distinct bearer tokens:

```json
{"site-001": "replace-with-long-random-secret"}
```

Use root ownership, group `ocpp`, and mode `0640`. Redeploy to enable the
collector. The dashboard does not require replication credentials and starts
on loopback for watchtower/control roles.

**Current limitations:** M4 does not yet install or configure the HTTPS/Basic
Auth reverse proxy; it must not be considered Internet-ready. A watchtower
with a local CSMS still needs local ingestion configured; selecting the role
alone does not enable ingestion. Do not merge until those items and
end-to-end deployment validation are complete.
