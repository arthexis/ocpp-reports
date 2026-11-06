# Optional mTLS for dashboard shortcuts

Dashboard authentication remains HTTP Basic by default. For a specific
dashboard, the Watchtower operator may add an `mtls_ca_file` property to
that dashboard's entry in the persistent root-owned
`/etc/ocpp-reports/dashboards.json` inventory:

```json
{
  "name": "dashboard-audi",
  "hostname": "audi.example.com",
  "port": 8082,
  "source_ids": ["site-001"],
  "title": "OCPP Reports",
  "htpasswd": "/etc/ocpp-reports/dashboard-audi.htpasswd",
  "mtls_ca_file": "/etc/ocpp-reports/dashboard-client-ca.pem"
}
```

The CA file must exist before applying Ansible. Use a **dedicated client CA**
for the approved device, not a public CA bundle. Issue a unique client
certificate and private key for gway-001; never commit keys, certificates
with private keys, passwords or token values to Git. Protect private keys
with user-only permissions and configure Chromium's
`AutoSelectCertificateForUrls` policy for the exact dashboard origins.
Do not put credentials in URL strings.

When a client presents a certificate that verifies against the configured
CA, the dashboard permits it; otherwise it falls back to Basic Auth.
The `auth_request` module must be present in Nginx. Verify the HTTPS
endpoint is actually terminated by this Nginx instance before enabling it.

## Rollout

1. Confirm the live TLS termination point and Nginx modules; check the
   existing Basic Auth path still challenges unauthenticated requests.
2. Provision dedicated CA trust anchor on Watchtower and a device-specific
   client certificate/key on gway-001 through an approved secret-delivery
   process. Do not transmit private key material in MCP output.
3. Opt in only the intended dashboard entries via `mtls_ca_file`.
4. Run Ansible, `nginx -t`, then test a verified client certificate, a
   noncertificate browser with Basic Auth, an invalid certificate, and
   unauthorized cross-dashboard access.
5. Configure Chromium certificate auto-selection on gway-001 and replace
   the Playwright desktop launcher only after real browser acceptance tests.
6. Record certificate expiration and revocation procedure; keep Basic Auth
   as a recovery route.

This change **does not** provision keys or enable mTLS in the default
inventory. Until the operator completes the rollout, desktop shortcuts
continue using their existing launcher.
