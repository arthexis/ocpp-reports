# B5: external systemd scheduling

This initial B5 chunk adds timezone-aware previous-period calculations and example systemd units. It does **not** yet implement `scheduled-run`; do not install or enable the units until the runner is implemented and validated.

`previous_period(now, cadence, zone)` returns UTC-aware half-open boundaries for the previous *completed* local day, week (Monday–Monday), or calendar month. Local midnight handles daylight-saving transitions correctly.

The sample timer triggers daily at 08:00 **in the systemd manager's local timezone**. `Persistent=true` catches missed calendar activations after shutdown; it does not replay every missed day individually. The runner must use a durable per-period key, refuse incomplete reports, and avoid duplicate sends.

Next chunks:
1. Implement a one-shot `scheduled-run` that reads an approved B2 internal summary, renders it through B3, queues via B4 and sends to the operator. Avoid assuming draft B2 has merged.
2. Provide durable per-period state and restart/replay tests.
3. Validate environment configuration, hardening, unit paths, and full CI; then enable deployment.
