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
REPORTS_COLLECTOR_URL=https://collector.gelectriic.com/api/replication
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

**Current limitations:** TLS proxy configuration is opt-in and requires operator-provided certificates and credentials. The deployment does not issue or renew certificates automatically. A watchtower with a local CSMS still needs local ingestion configured; selecting the role alone does not enable ingestion.

## Separate HTTPS hosts (watchtower/control)

The existing CSMS virtual host `porsche-abb.gelectriic.com` is untouched.
Reports manages two different Nginx sites, using independent certificates:

- `porsche.gelectriic.com`: Basic Auth dashboard, loopback port 8080.
- `collector.gelectriic.com`: only `POST /api/replication`, source bearer authentication, loopback port 8081.

Provision both certificates (for example using Certbot on the server) before
enabling HTTPS. Certificate issuance and renewal remain operator-managed.
Both DNS names must resolve to this server. Configure the Collector credentials
and dashboard htpasswd before deploying. For example:

Install Nginx, Certbot and `apache2-utils` using the distribution packages.
Issue certificates for both hostnames using a compatible Certbot challenge
method (for example `certbot certonly --nginx -d HOSTNAME` if an existing
working Nginx configuration supports HTTP-01). Do not stop or replace the
existing CSMS Nginx service to obtain certificates.

Create the configuration directory after an initial Reports deployment
(which provisions the `ocpp` group), then create the dashboard password
and collector credentials:

```sh
sudo htpasswd -c /etc/ocpp-reports/dashboard.htpasswd operator
sudo chown root:www-data /etc/ocpp-reports/dashboard.htpasswd
sudo chmod 0640 /etc/ocpp-reports/dashboard.htpasswd
# Create /etc/ocpp-reports/collector-credentials.json:
# {"site-001":"<long unique random bearer token>"}
sudo chown root:ocpp /etc/ocpp-reports/collector-credentials.json
sudo chmod 0640 /etc/ocpp-reports/collector-credentials.json
OCPP_REPORTS_NGINX_ENABLED=true ./deploy.sh --watchtower
```

The HTTPS deployment refuses missing credentials, absent certificates,
inactive Collector/Dashboard services, and an enabled legacy combined
`/etc/nginx/sites-enabled/ocpp-reports` site. It does not disable or
rewrite that legacy site automatically. Resolve it manually before enabling
the new split hosts. Do not alter the CSMS site.

The certificate paths default to
`/etc/letsencrypt/live/{hostname}/fullchain.pem` and `privkey.pem`.
Override with `OCPP_REPORTS_DASHBOARD_TLS_CERT`,
`OCPP_REPORTS_DASHBOARD_TLS_KEY`, `OCPP_REPORTS_COLLECTOR_TLS_CERT`,
`OCPP_REPORTS_COLLECTOR_TLS_KEY` as needed. Hostnames can be overridden
using `OCPP_REPORTS_DASHBOARD_HOSTNAME` and
`OCPP_REPORTS_COLLECTOR_HOSTNAME`. The two hostnames must be different.

Nginx must be reloaded after Certbot renews certificates. Validate the
complete configuration with `sudo nginx -t`. The installer does not
modify the CSMS site or global Nginx configuration. Confirm coexistence with CSMS and perform live endpoint validation before production exposure.

## Optional local CSMS ingestion (watchtower/control)

The existing `ingest --follow` command performs an initial catch-up and
then polls for new CSMS evidence. M4 can run it as a dedicated systemd
service, independently of the collector and dashboard. It is **opt-in**:
a remote-only collector should not require access to a local CSMS.

Before enabling, identify the actual CSMS schema-8 evidence directory
; the producer is resolved from the running `ocpp-csms.service` console script. The Reports service user must be able
to read the CSMS SQLite files and traverse their parent directories.
The producer runs under CSMS's own installed Python interpreter. The installer does not modify CSMS or its DB.

```sh
export OCPP_REPORTS_LOCAL_INGEST_ENABLED=true
export OCPP_REPORTS_CSMS_DATA_DIR=/actual/path/to/csms/schemas/8
export OCPP_REPORTS_LOCAL_INGEST_INTERVAL=30
./deploy.sh --watchtower
sudo systemctl status ocpp-reports-ingest.service
sudo journalctl -u ocpp-reports-ingest.service -n 80 --no-pager
```

Keep these environment settings in the operator's deployment configuration
for subsequent redeploys. The Reports service user must be able to inspect the
active CSMS process and read the evidence directory; import failures are
reported in the journal and cause visible service restarts. The service never writes to CSMS.
