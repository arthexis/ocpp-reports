"""Deterministic, safe CSV projection of the established summary/v1 transactions."""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

COLUMNS = ("id", "charger_id", "connector_id", "state", "active", "rfid",
           "started_at", "stopped_at", "energy_wh", "completeness")


def _safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        raise ValueError("CSV cell must be scalar")
    text = str(value)
    if text and text.lstrip().startswith(("=", "+", "-", "@")) and not isinstance(value, (int, float)):
        return "'" + text
    return text


def render_csv(report: dict[str, Any]) -> str:
    """One row per transaction; absent measurements remain empty, never zero."""
    if report.get("schema") != "ocpp-reports/summary/v1":
        raise ValueError("Unsupported report schema")
    if not isinstance(report.get("summary"), dict) or not isinstance(report.get("period"), dict):
        raise ValueError("Malformed summary or period")
    rows = report.get("transactions")
    if not isinstance(rows, list):
        raise ValueError("Transactions must be a list")
    seen = set()
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None or not row.get("charger_id"):
            raise ValueError("Transaction requires id and charger_id")
        identity = (str(row["charger_id"]), str(row["id"]))
        if identity in seen:
            raise ValueError("Duplicate transaction identity")
        seen.add(identity)
        if row.get("energy_wh") is not None and (isinstance(row["energy_wh"], bool) or not isinstance(row["energy_wh"], (int, float))):
            raise ValueError("energy_wh must be numeric or null")
        normalized.append(row)
    normalized.sort(key=lambda row: (str(row["charger_id"]), str(row["id"])))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in normalized:
        complete = row.get("energy_wh") is not None and row.get("state") == "completed"
        cells = [row.get(column) if column != "completeness" else ("complete" if complete else "provisional") for column in COLUMNS]
        writer.writerow([_safe(cell) for cell in cells])
    return output.getvalue()
