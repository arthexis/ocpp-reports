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
                             cadence: str = 'daily', zone: str = 'UTC', now: datetime | None = None,
                             since: datetime | None = None, until: datetime | None = None,
                             output: Path | None = None, queue: bool = True, format: str = "text",
                             document: dict | None = None) -> str:
    """Queue only a complete, exact-period report; never invoke SMTP here."""
    now = now or datetime.now(timezone.utc)
    if (since is None) != (until is None):
        raise ValueError('Both since and until must be specified')
    if since is not None:
        if since.tzinfo is None or until.tzinfo is None or since >= until:
            raise ValueError('Explicit period must be timezone-aware and ordered')
        start, end = since.astimezone(timezone.utc), until.astimezone(timezone.utc)
    else:
        start, end = previous_period(now, cadence=cadence, zone=zone)
    if document is None:
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
    if not operator or "@" not in operator or "\r" in operator or "\\n" in operator:
        raise ValueError("Valid operator email required")
    if format not in ("text", "csv"):
        raise ValueError("Unsupported manual report format")
    if queue and format != "text":
        raise ValueError("SMTP delivery supports text reports only")
    rendered = render_report(document, format=format)
    subject = f"OCPP report {cadence}: {start.date().isoformat()} to {end.date().isoformat()}"
    if output is not None:
        with output.open('x', encoding='utf-8') as stream:
            stream.write(rendered)
    if not queue:
        return rendered
    period_key = f'{operator}|{document["source_id"]}|{start.isoformat()}|{end.isoformat()}'
    key, created = outbox.enqueue_period(period_key=period_key, recipient=operator,
                                         subject=subject, body=rendered, revision=revision)
    return key if created else f'EXISTS:{key}'
