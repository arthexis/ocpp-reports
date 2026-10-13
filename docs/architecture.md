# Reporting architecture and contract gates

## Ownership
CSMS owns OCPP communication, transactions, evidence and control. Reports reads **versioned export files** only; it must never write into CSMS storage, import CSMS private modules, or execute CSMS CLI commands.

## Dependency policy
- Pydantic 2: typed validation and JSON schema for reports-owned metadata. **Do not model assumed CSMS fields.**
- Typer: command entry point and argument validation.
- Jinja2: optional HTML/text presentation; templates are packaged now, rendering arrives in B3.
- Python standard library: JSON, CSV, zoneinfo, SQLite, SMTP/email and filesystem primitives.
- pytest/pytest-cov: development-only.
- systemd timers: deployment-time scheduling, never an embedded scheduler.

## Export reconnaissance (B0a, not yet completed)
Inspect the *real* CSMS export producer and sanitized samples, then record actual field names, identifiers, types, timestamps, units, nullability and version. The string `ocpp-csms/transactions/v1` is only a candidate. Do not implement the B2 adapter until verified fixtures and producer compatibility decisions are recorded.

## Time and identity (B0c design)
Reporting periods are timezone-aware, half-open [since, until). Keep source event time, imported-at and generated-at separately. An arrival or correction after a report was generated must produce a traceable revision rather than silently replacing an already-delivered artifact. A logical report identity comprises type, scope, timezone, period and relevant configuration; revision identity derives from evidence/configuration fingerprints. Delivery idempotency must additionally distinguish recipient and channel.

## Execution and delivery
Commands are one-shot; `generate` intentionally exits nonzero until B2 exists, and never pretends to have generated an output. Use systemd timers to schedule, SQLite to persist reports-owned outbox state, and standard-library SMTP for optional delivery in B4. No background worker, no network required for local reporting.
