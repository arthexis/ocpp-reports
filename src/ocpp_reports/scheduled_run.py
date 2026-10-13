"""One-shot scheduled report from an already-verified internal summary."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .rendering import render_report
from .schedule import previous_period
from .outbox import Outbox


def prepare_scheduled_report(*, source: Path, outbox: Outbox, operator: str,
                             cadence: str, zone: str, now: datetime | None = None) -> str:
    """Queue only a complete, exact-period report; never invoke SMTP here."""
    now = now or datetime.now(timezone.utc)
    start, end = previous_period(now, cadence=cadence, zone=zone)
    document = json.loads(source.read_text(encoding="utf-8"))
    if document.get("schema") != "ocpp-reports/summary/v1":
        raise ValueError("Unsupported internal report schema")
    period = document.get("period")
    summary = document.get("summary")
    if not isinstance(period, dict) or not isinstance(summary, dict):
        raise ValueError("Missing report period or summary")
    def utc(value: str) -> datetime:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Report timestamps must be timezone-aware")
        return result.astimezone(timezone.utc)
    if utc(period["since"]) != start or utc(period["until"]) != end:
        raise ValueError("Report period does not match previous completed interval")
    if summary.get("completeness") != "complete" or summary.get("missing_energy_transactions") != 0:
        raise ValueError("Refusing to schedule incomplete or provisional report")
    revision = document.get("revision")
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("Stable report revision required")
    if not operator or "@" not in operator or "\r" in operator or "\n" in operator:
        raise ValueError("Valid operator email required")
    rendered = render_report(document, format="text")
    subject = f"OCPP report {cadence}: {start.date().isoformat()} to {end.date().isoformat()}"
    return outbox.enqueue(recipient=operator, subject=subject, body=rendered, revision=revision)
