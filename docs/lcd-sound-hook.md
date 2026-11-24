# OCPP Reports → gway-001 LCD/Sound hook

## Read-only production integration

- The **independent observer** runs as the existing authenticated ABB sender
  `ubuntu`, not under the `ocpp-reports` collector/ingest process identity.
  The warehouse remains **read-only**: SQLite `mode=ro`, `PRAGMA query_only`.
  On ABB, `ubuntu` is already a member of the `ocpp` group and an independent
  read-only `sqlite3` probe has confirmed access.
- This reuses `/home/ubuntu/.config/gway-lcd-sound/server-source.json` and its
  existing verified Watchtower bearer **without copying credentials, bypassing
  authentication or provisioning another principal**. Only the explicit
  `lcd.reports` operation is added to the incumbent `ocpp-abb-1` principal's
  Watchtower grant; `sound.play` and `sound.status` were already allowed.
  The Watchtower root-owned grant still restricts the destination to `gway-001`.
- The observer is a distinct systemd unit, never part of the collector/ingest
  service. `ProtectSystem=strict`, `ProtectHome=read-only` and a private
  `StateDirectory=ocpp-reports-lcd` restrict side effects to the hook journal
  and existing authenticated `lcd`/`sound` CLIs.
- No new SSH, WireGuard, credential flow, DB schema, event-cursor mutation
  or forwarder changes are required.

## Display and audio contract

- Every 30 seconds build a source-scoped, bounded Reports snapshot from
  immutable warehouse observations, and forward it through the public
  `lcd reports <frames-json>` command. Each snapshot has a 75-second lease.
- Up to 16 connector screens rotate every 10 seconds. Priority:
  undervoltage > actions-runner > existing event overlays > Reports
  > default LCD rotation. Higher-priority events always interrupt.
- The first row is a short location and elapsed minutes (`NNNm`, capped at
  `999m`), with exactly 16 characters maximum. For precisely two words, only
  the initial of the first is retained, then the tail is trimmed if necessary.
  Unknown duration displays `---m`.
- The second row is `Preparing`, `Charging NN%` (only verified fresh OCPP
  meter samples), `Complete` for 120 seconds from a verified stop, or a
  confirmed station-wide fault. A connector C1 Faulted while C0 is Available
  must **not** create a fault alert. A verified EmergencyStop is reported
  independently from an incidental connector-only Faulted status.
- Exact connector transitions are journaled before any physical sound effect.
  The global sound monitor suppresses ordinary Reports LCD takeovers; the
  hook emits one sound for each true state change and no sound for unchanged
  refreshes, rotation, time increments or SoC updates. No retry after an
  uncertain physical delivery.

## Deployment / rollback (ABB)

1. Merge and deploy GWay Remote `lcd.reports` receiver to Watchtower and
   gway-001, then install current gway-lcd-sound on gway-001 and restart
   `lcd-lockfile.service`. Retain previous release for rollback.
2. Confirm `ubuntu` has its established LCD/Sound route to gway-001 and can
   read `/var/lib/ocpp-reports/warehouse.sqlite` read-only.
3. Add `lcd.reports` to **only** the existing ocpp-abb-1 sender's trusted
   Watchtower grant, preserving its current principal and other operations.
   Verify `lcd.show` remains independently authorized, and unrelated
   principals gain no operation.
4. Deploy the Reports release to ABB by the existing `Deploy OCPP to ABB`
   GitHub workflow. Install the pinned unit from
   `ansible/templates/ocpp-reports-lcd-hook.service.j2` as
   `/etc/systemd/system/ocpp-reports-lcd-hook.service`.
5. Run `ocpp-reports lcd-hook --once` under `ubuntu` for first silent
   baseline; then enable the new service. Check the receiver's Reports frame
   journal and existing CSMS/Reports service health.

For rollback, `systemctl disable --now ocpp-reports-lcd-hook.service`,
remove the `lcd.reports` grant from ABB (not `sound.play` or other operations),
and restore the previous LCD/Sound receiver release if necessary. Its 75s TTL
automatically clears any outstanding Reports frame. No replay of uncertain
physical requests.

The production warehouse may have no active sessions; do not fabricate charge
events or claim a real-session visual/sound confirmation without live data.
Use hermetic status transitions and a bounded local LCD canary to verify behavior.
