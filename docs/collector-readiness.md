# Authenticated collector readiness (#108)

Before first forwarding or any source identity cutover, update the existing Watchtower `collector.toml`, apply the collector's configured reload/restart during a coordinated window, and verify the *running* collector from the satellite:

```sh
ocpp-reports forwarder check --remote --json
```

The check reads the protected local `forwarder.toml`, uses normal verified HTTPS to the configured `/api/replication` endpoint with `/ready` appended, and sends the source ID and existing bearer token to `POST /api/replication/ready`. It never sends a replication batch, mutates the local ACK, creates a collector database, or modifies a collector cursor. A response with `schema=ocpp-reports/readiness/v1`, matching source and `ready=true` confirms loaded credentials and an unheld source; it does not authorize forwarding or prove history synchronization.

An authentication failure (401) can indicate a token mismatch or a running collector that predates a configuration change. Check config changes against service start time, reload/restart **only the collector** under operator control, then repeat the probe. HTTP 404/405 means unsupported installed collector: deploy the collector update first. HTTP 503 means held: preserve the operator's hold until explicit release. TLS/network failures must not be bypassed with insecure verification.

The collector must expose `POST /api/replication/ready` through its existing HTTPS Nginx proxy. Deploys must explicitly verify this URL is forwarded, including Nginx method restrictions; a Python-only test does not prove external routing.

Run the probe after Watchtower credential changes and before enabling a new satellite forwarder. Normal forwarder operation continues using existing backoff/retry behavior; transient probe failures must not automatically stop existing charging or replication.
