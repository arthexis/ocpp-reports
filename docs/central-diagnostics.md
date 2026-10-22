# Central diagnostic archives

OCPP CSMS retains ownership of GetDiagnostics requests and charger-facing HTTPS uploads. OCPP Reports owns satellite-to-Collector archive transfer, immutable central copies and authenticated operator downloads at `diagnose.gelectriic.com`.

## Satellite

Deploy Reports with the `satellite` role (or `control` with local ingest enabled). Configure the existing Forwarder endpoint, source ID, and bearer token using the Forwarder configuration procedure. The Reports diagnostic synchronization timer uses the same credential file, without adding a second secret or a second replication cursor.

The one-shot service runs as the configured local Reports operator so that CSMS owner-only diagnostic request records remain protected. **The Reports operator must be the same account with read access to the locally installed CSMS diagnostics metadata and uploads.** No permissions on those files are widened by the Reports deployment.

The timer invokes `ocpp-reports diagnose forward` every five minutes. It discovers the active CSMS installation, reads only attributed request records and regular, non-symlink archive files, and uploads them using HTTPS and the existing source bearer token. Each acknowledgement is verified by SHA-256. Delivered identities are recorded durably; interrupted transfers are retried without forwarding duplicate records.

The timer is a separate, bounded one-shot unit, not a second long-running process. Failures never advance the JSON replication cursor or call CSMS controls. The five-minute cadence provides a simple retry backoff without accumulating jobs. Review `systemctl status ocpp-reports-diagnostics-forward.timer` and `journalctl -u ocpp-reports-diagnostics-forward.service` for health.

## Watchtower

Deploy Reports with the `watchtower` role, set `OCPP_REPORTS_NGINX_ENABLED=true` when ready for public exposure, and provision certificates for the dashboard, Collector and diagnostics hosts. The diagnostics hostname defaults to `diagnose.gelectriic.com`; override with `OCPP_REPORTS_DIAGNOSTICS_HOSTNAME`. Its cert and key can be provided through `OCPP_REPORTS_DIAGNOSTICS_TLS_CERT` and `OCPP_REPORTS_DIAGNOSTICS_TLS_KEY`. **The required TLS certificate must exist before nginx exposure is enabled.** Cert issuance is not performed by the `diagnose serve` command.

The Collector accepts a bounded 100 MiB archive with a per-source bearer token at `POST /api/diagnostics`. The body is not part of the JSON warehouse replication feed. The central archive index is Reports-owned at `/var/lib/ocpp-reports/diagnostics`.

Use the operator CLI on Watchtower:

```sh
sudo ocpp-reports diagnose serve --cp CP001
sudo ocpp-reports diagnose serve --cp CP001 --status
sudo ocpp-reports diagnose serve --cp CP001 --revoke
```

If multiple satellites have the same CP ID, pass `--source SOURCE_ID` when publishing. The generated username is the CP ID and its password rotates with each `serve` invocation. Revocation deletes publication credentials and copies, never stored original archives. The nginx host includes only Reports-managed location snippets and returns 404 otherwise.

## Migration and safety

1. Configure and deploy the Watchtower Reports Collector before any satellite begins forwarding files.
2. Configure TLS certificates and enable Reports nginx. Reports removes the **known old CSMS diagnostics virtual host** only when its contents match the expected CSMS template; it refuses to overwrite an unrelated site.
3. Deploy the satellite Reports changes and verify an archive is acknowledged and listed at Watchtower.
4. Merge and deploy the CSMS removal PR to eliminate the obsolete CSMS CLI and deployment surface.
5. Revoke any old credentials from the retired CSMS sharing site and review inaccessible legacy published copies for retention cleanup. The new Reports publication credentials are independent.

CSMS expiration of an upload capability is different from archive retention. Expired original upload URLs are retired while received archives remain locally for up to thirty days, allowing offline forwarding. After that interval, copies already delivered to Watchtower remain available according to Reports' archive retention policy. Central archive retention and field-tested deletion policy should be configured before unattended high-volume operation.

No process writes to CSMS SQLite, reconstructs OCPP events, or changes charger session state.
