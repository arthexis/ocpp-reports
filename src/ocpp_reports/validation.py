"""Shared structural and measurement validation for summary/v1 renderers."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an offset-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an offset-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be an offset-aware ISO timestamp")
    return parsed


def _count(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")


def validate_summary(report: Any) -> dict[str, Any]:
    """Validate once before HTML, text or CSV; never coerce missing energy to zero."""
    if not isinstance(report, dict):
        raise ValueError("Report must be an object")
    if report.get("schema") != "ocpp-reports/summary/v1":
        raise ValueError("Unsupported report schema")
    if not isinstance(report.get("source_id"), str) or not report["source_id"].strip():
        raise ValueError("source_id must be nonempty")
    period, summary, rows = report.get("period"), report.get("summary"), report.get("transactions")
    if not isinstance(period, dict) or not isinstance(summary, dict) or not isinstance(rows, list):
        raise ValueError("Malformed report period, summary or transactions")
    start = _timestamp(period.get("since"), "period.since")
    end = _timestamp(period.get("until"), "period.until")
    if start >= end:
        raise ValueError("Report period must be ordered")
    if not isinstance(period.get("timezone"), str) or not period["timezone"]:
        raise ValueError("Report timezone is required")
    for field in ("transactions", "known_energy_wh", "missing_energy_transactions"):
        _count(summary.get(field), f"summary.{field}")
    if summary.get("completeness") not in ("complete", "partial"):
        raise ValueError("Invalid summary completeness")
    if summary["transactions"] != len(rows):
        raise ValueError("Summary transaction count mismatch")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Transaction must be an object")
        identity, charger = row.get("id"), row.get("charger_id")
        if isinstance(identity, bool) or not isinstance(identity, int):
            raise ValueError("Transaction id must be an integer")
        if not isinstance(charger, str) or not charger:
            raise ValueError("Transaction requires charger_id")
        key = (charger, identity)
        if key in seen:
            raise ValueError("Duplicate transaction identity")
        seen.add(key)
        energy = row.get("energy_wh")
        if energy is not None:
            _count(energy, "energy_wh")
        for field in ("started_at", "stopped_at"):
            if row.get(field) is not None:
                _timestamp(row[field], field)
        if row.get("started_at") is not None and row.get("stopped_at") is not None:
            if _timestamp(row["started_at"], "started_at") > _timestamp(row["stopped_at"], "stopped_at"):
                raise ValueError("Transaction timestamps out of order")
        for field in ("state", "rfid"):
            if row.get(field) is not None and not isinstance(row[field], str):
                raise ValueError(f"{field} must be a string or null")
        if row.get("active") is not None and not isinstance(row["active"], bool):
            raise ValueError("active must be boolean or null")
        if row.get("connector_id") is not None:
            _count(row["connector_id"], "connector_id")
    return report
