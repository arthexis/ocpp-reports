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
            selected.append({"id": txid, "charger_id": tx.charger_id, "connector_id": tx.connector_id, "rfid": tx.rfid, "state": tx.state, "active": tx.active, "started_at": tx.started_at, "stopped_at": tx.stopped_at, "energy_wh": tx.energy_wh, "meter_start_wh": getattr(tx, "meter_start_wh", None), "meter_stop_wh": getattr(tx, "meter_stop_wh", None), "stop_reason": getattr(tx, "stop_reason", None)})
    total_wh = sum(row["energy_wh"] for row in selected if row["energy_wh"] is not None)
    missing = sum(row["energy_wh"] is None for row in selected)
    report = {"schema": "ocpp-reports/summary/v1", "source_id": next(iter(sources)), "period": {"since": since.isoformat(), "until": until.isoformat(), "timezone": tz, "semantics": "[since,until)"}, "summary": {"transactions": len(selected), "known_energy_wh": total_wh, "missing_energy_transactions": missing, "completeness": "partial" if missing or any(row["active"] or row["state"] not in {"completed", "stopped"} for row in selected) else "complete"}, "transactions": selected, "evidence": evidence}
    report["revision"] = sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return report


def build_report_from_database(database: Path, since: datetime, until: datetime,
                               tz: str, *, source_id: str | None = None) -> dict:
    """Build a report from Reports-owned SQLite, never the producer database."""
    from .transaction_projection import current_transactions
    from .rendering import render_report
    ZoneInfo(tz)
    if (since.tzinfo is None or until.tzinfo is None
            or since.utcoffset() is None or until.utcoffset() is None
            or since >= until):
        raise ValueError("Expected ordered timezone-aware half-open period")
    observations = current_transactions(database, source_id=source_id)
    sources = {item["source_id"] for item in observations}
    if source_id is None:
        if len(sources) != 1:
            raise ValueError("Specify --source-id for an empty or multi-source database")
        source_id = next(iter(sources))
    selected = []
    evidence = []
    start, end = since.astimezone(timezone.utc), until.astimezone(timezone.utc)
    for item in observations:
        tx = item["transaction"]
        at = parse_time(tx.get("started_at")) or parse_time(tx.get("last_activity_at"))
        if at is None or not start <= at < end:
            continue
        selected.append({key: tx.get(key) for key in
                         ("id", "charger_id", "connector_id", "rfid", "state",
                          "active", "started_at", "stopped_at", "energy_wh",
                          "meter_start_wh", "meter_stop_wh", "stop_reason")})
        evidence.append({"charger_id": tx["charger_id"], "transaction_id": tx["id"],
                         "page_next": item["evidence"]["page_next"],
                         "fingerprint": item["evidence"]["fingerprint"]})
    selected.sort(key=lambda row: (row["charger_id"], row["id"]))
    evidence.sort(key=lambda row: (row["charger_id"], row["transaction_id"]))
    missing = sum(row["energy_wh"] is None for row in selected)
    report = {
        "schema": "ocpp-reports/summary/v1", "source_id": source_id,
        "period": {"since": since.isoformat(), "until": until.isoformat(),
                   "timezone": tz, "semantics": "[since,until)"},
        "summary": {
            "transactions": len(selected),
            "known_energy_wh": sum(row["energy_wh"] for row in selected if row["energy_wh"] is not None),
            "missing_energy_transactions": missing,
            "completeness": "partial" if missing or any(row["active"] or row["state"] not in {"completed", "stopped"} for row in selected) else "complete",
        },
        "transactions": selected, "evidence": evidence,
    }
    report["revision"] = sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    render_report(report, "csv")  # Shared summary validation before returning.
    return report

def render(report: dict, fmt: str, *, profile: str | None = None, locations: dict | None = None) -> str:
    if profile is not None:
        if fmt != "csv" or profile != "audi":
            raise ValueError("Profile audi requires CSV format")
        from .csv_profiles import render_audi
        return render_audi(report, locations=locations)
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=True) + "\n"
    if fmt == "audi-csv":
        from .profiles import render_audi_csv
        return render_audi_csv(report)
    if fmt == "csv":
        from .rendering import render_report
        return render_report(report, "csv")
    raise ValueError(f"Unsupported format: {fmt}")
