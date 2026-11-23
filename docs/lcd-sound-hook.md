# OCPP Reports -> gway-001 LCD/Sound hook

The Reports service account `ocpp-reports` is a real event producer. It must have
its **own** authenticated, revocable GWay LCD/Sound sender principal on
`ocpp-abb-1`, bound to origin `ocpp-abb-1` and target `gway-001`.
Do not copy the Watchtower or `ubuntu` bearer into this account.

## Source and display contracts

- Poll Reports' existing warehouse, SQLite **mode=ro**, every 30s. No dashboard
  login bypass, CSMS reads/writes, event-cursor mutations or network API.
- Source freshness is mandatory (last ingestion within 180s); a stale source
  stops renewing the LCD channel, which expires on the receiver in 75s.
- Screens for Preparing/Charging use the short site label plus elapsed minutes.
  The first of exactly two location words becomes a single initial. The final
  four chars are reserved for `NNNm`; duration saturates at 999m, and
  uncertain duration is `---m`, never a guessed value.
- Verified transaction end gives Complete for 120s from `stopped_at`; false
  completion on disconnect or missing polls is prohibited.
- C1 Faulted with healthy/unknown station C0 is not a station-wide alarm.
  Confirmed C0 Faulted/Unavailable is displayed; verified StopTransaction
  reason EmergencyStop is treated as a station-wide alarm independently of C0.
- A pure receiver Reports channel rotates up to 16 connector screens every
  10s. Priority: undervoltage > actions runner > existing event overlays >
  Reports > normal LCD script/cycler.
- One sound for a **new** transition of exact source/charger/connector, not for
  rotation, SoC changes, minute ticks or the same state every 30 seconds.
  Startup baselines silently. The last states are committed before external
  effects: uncertain audio or LCD ACK is never retried, so an outage can lose
  a notification but cannot duplicate physical sounds.

## Provisioning (separate reviewed field step)

1. Deploy the `gway-lcd-sound` Reports channel to `gway-001`; deploy the
   compatible `gway-remote` receiver to 001 and Watchtower first. Verify
   receiver allowlist and all CI checks.
2. Install `gway-lcd-sound` as the **ocpp-reports account** on ABB, under
   that account's own home. Configure its standard source route for LCD/Sound
   to `gway-001`, using the audited source setup helper. Do not use
   `ubuntu`'s environment. Install dedicated source bearer via the existing
   secrets lifecycle, never in git or a PR.
3. On Watchtower, issue an independent verified principal with the dedicated
   `gway:lcd-sound` bearer scope. Add only `lcd.reports` and `sound.play`
   operations for `ocpp-abb-1` -> `gway-001` to the root-controlled
   source grants. Keep existing `ubuntu` and Watchtower principals intact.
   Confirm revocation removes access without a local-hardware fallback.
4. Install `ansible/templates/ocpp-reports-lcd-hook.service.j2` as
   `/etc/systemd/system/ocpp-reports-lcd-hook.service`, reload systemd,
   then run `ocpp-reports lcd-hook --once` under the exact service account.
   Inspect receipts and ensure no audio on initial baseline.
5. Enable/start the unit **only after** step 4 and independent LCD observation.
   Do not interrupt `ocpp-reports-ingest`, OCPP-CSMS, SSH, WireGuard or
   existing Watchtower gateway/Odoo services.

## Verification

- With two or more physical connectors charging, inspect receiver
  `lcd-current.json` and bounded recent display history; screens should
  alternate at ~10-second intervals. Status updates arrive at ~30s.
- Check that an existing higher-priority overlay interrupts and the carousel
  resumes after its TTL. Never overwrite `lcd-event-*.lck` from the hook.
- Use a test source transition on a hermetic fixture, then one controlled
  real state transition: exactly one audio event per new status; none on
  poll/rotation/restart, and no false failure for ABB C0 Available/C1 Faulted.
- Revocation, expiry (75s), and rollback of each package have a documented
  exit: stop/disable Reports hook, revoke its principal, and restore each
  release from its previous symlink. Never replay an unknown outcome.

No credentials, route config, token or runtime readings belong in the PR.
