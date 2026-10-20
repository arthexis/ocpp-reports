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

## Separate HTTPS hosts (watchtower/control)

The existing CSMS virtual host `porsche-abb.gelectriic.com` is untouched.
Reports manages two different Nginx sites, using independent certificates:

- `porsche.gelectriic.com`: Basic Auth dashboard, loopback port 8080.
- `collector.gelectriic.com`: only `POST /api/replication`, source bearer authentication, loopback port 8081.

Provision both certificates (for example using Certbot on the server) before
enabling HTTPS. Certificate issuance and renewal remain operator-managed.
Both DNS names must resolve to this server. Configure the Collector credentials
and dashboard htpasswd before deploying. For example:

```sh
sudo apt-get install certbot apache2-utils
sudo certbot certonly --webroot -w /var/www/html -d collector.gelectriic.com
sudo certbot certonly --webroot -w /var/www/html -d porsche.gelectriic.com
sudo htpasswd -c /etc/ocpp-reports/dashboard.htpasswd operator
sudo chown root:www-data /etc/ocpp-reports/dashboard.htpasswd
sudo chmod 0640 /etc/ocpp-reports/dashboard.htpasswd
OCPP_REPORTS_NGINX_ENABLED=true ./deploy.sh --watchtower
```

The Certbot `--webroot` commands require an existing Nginx HTTP
challenge location serving `/var/www/html/.well-known/acme-challenge/`.
If that is not already configured, use a compatible DNS challenge or
another supported certificate issuance method instead. Do not assume the
commands above will work on an unconfigured host.

The certificate paths default to
`/etc/letsencrypt/live/{hostname}/fullchain.pem` and `privkey.pem`.
Override with `OCPP_REPORTS_DASHBOARD_TLS_CERT`,
`OCPP_REPORTS_DASHBOARD_TLS_KEY`, `OCPP_REPORTS_COLLECTOR_TLS_CERT`,
`OCPP_REPORTS_COLLECTOR_TLS_KEY` as needed. Hostnames can be overridden
using `OCPP_REPORTS_DASHBOARD_HOSTNAME` and
`OCPP_REPORTS_COLLECTOR_HOSTNAME`. The two hostnames must be different.

Nginx must be reloaded after Certbot renews certificates. Validate the
complete configuration with `sudo nginx -t`. The installer does not
modify the CSMS site or global Nginx configuration. Do not merge until
coexistence and deployment tests have passed.
