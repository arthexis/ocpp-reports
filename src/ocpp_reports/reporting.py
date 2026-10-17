"""Deterministic reporting from immutable CSMS export snapshots."""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
from zoneinfo import ZoneInfo
from .export import read_page, Transaction

def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Source timestamp has no timezone offset")
    return dt.astimezone(timezone.utc)

def build_report(source: Path, since: datetime, until: datetime, tz: str) -> dict:
    ZoneInfo(tz)
    if since.tzinfo is None or until.tzinfo is None or since >= until:
        raise ValueError("Expected ordered timezone-aware half-open period")
    pages = [source] if source.is_file() else sorted(source.glob("*.json"))
    if not pages:
        raise ValueError("No export JSON pages found")
    by_identity: dict[tuple[str, int], Transaction] = {}
    sources = set()
    evidence = []
    for path in pages:
        page = read_page(path)
        sources.add(page.data.source_id)
        evidence.append({"name": path.name, "sha256": sha256(path.read_bytes()).hexdigest()})
        for tx in page.data.transactions:
            by_identity[(page.data.source_id, tx.id)] = tx
    if len(sources) != 1:
        raise ValueError("Multiple CSMS sources require separate reports")
    start, end = since.astimezone(timezone.utc), until.astimezone(timezone.utc)
    selected = []
    for (_, txid), tx in sorted(by_identity.items()):
        at = parse_time(tx.started_at) or parse_time(tx.last_activity_at)
        if at is not None and start <= at < end:
            selected.append({"id": txid, "charger_id": tx.charger_id, "connector_id": tx.connector_id, "rfid": tx.rfid, "state": tx.state, "active": tx.active, "started_at": tx.started_at, "stopped_at": tx.stopped_at, "energy_wh": tx.energy_wh})
    total_wh = sum(row["energy_wh"] for row in selected if row["energy_wh"] is not None)
    missing = sum(row["energy_wh"] is None for row in selected)
    report = {"schema": "ocpp-reports/summary/v1", "source_id": next(iter(sources)), "period": {"since": since.isoformat(), "until": until.isoformat(), "timezone": tz, "semantics": "[since,until)"}, "summary": {"transactions": len(selected), "known_energy_wh": total_wh, "missing_energy_transactions": missing, "completeness": "partial" if missing else "complete"}, "transactions": selected, "evidence": evidence}
    report["revision"] = sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return report

def render(report: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=True) + "\n"
    if fmt == "csv":
        from .rendering import render_report
        return render_report(report, "csv")
    raise ValueError(f"Unsupported format: {fmt}")
