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

**Current limitations:** TLS proxy configuration is opt-in and requires operator-provided certificates and credentials. The deployment does not issue or renew certificates automatically. A watchtower
with a local CSMS still needs local ingestion configured; selecting the role
alone does not enable ingestion. Do not merge until those items and
end-to-end deployment validation are complete.

## Optional Nginx HTTPS exposure (watchtower / control only)

Provision a valid certificate and matching private key, and create a Basic Auth
password file using `htpasswd` (package `apache2-utils`):

```sh
sudo htpasswd -c /etc/ocpp-reports/dashboard.htpasswd operator
sudo chown root:www-data /etc/ocpp-reports/dashboard.htpasswd
sudo chmod 0640 /etc/ocpp-reports/dashboard.htpasswd
```

Create the Collector bearer-token JSON file as described above **before**
enabling public exposure. Set these environment variables when running deploy:

```sh
OCPP_REPORTS_NGINX_ENABLED=true \
OCPP_REPORTS_NGINX_SERVER_NAME=reports.example.org \
OCPP_REPORTS_TLS_CERT=/etc/letsencrypt/live/reports.example.org/fullchain.pem \
OCPP_REPORTS_TLS_KEY=/etc/letsencrypt/live/reports.example.org/privkey.pem \
./deploy.sh --watchtower
```

Nginx serves the dashboard over HTTPS with Basic Auth on all routes except
`POST /api/replication`, which passes the satellite bearer token to the
Collector. This distinction is intentional: satellites do not receive the
dashboard operator password. Both Python servers remain on loopback. HTTP
port 80 redirects to HTTPS.

The installer refuses to configure public exposure if certificate, key,
htpasswd, or Collector credentials are missing. It runs `nginx -t` before
reloading. Certificate issuance and renewal, DNS, and firewall policy are
operator-managed. If an existing Nginx virtual host uses the same name or
ports, reconcile it before enabling this site.
