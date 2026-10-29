"""Deterministic, safe CSV projection of the established summary/v1 transactions."""
from __future__ import annotations

import csv
import io
from typing import Any
from .validation import validate_summary

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
    if text and text.lstrip("\t\r\n \ufeff").startswith(("=", "+", "-", "@")) and not isinstance(value, (int, float)):
        return "'" + text
    return text


def _record(cells) -> str:
    output = io.StringIO(newline="")
    # Include both CR and LF in CSV's quoting rules, then normalize only the
    # record terminator. Embedded carriage returns and CRLF remain intact.
    csv.writer(output, lineterminator="\r\n").writerow(cells)
    return output.getvalue()[:-2] + "\n"


def render_csv(report: dict[str, Any]) -> str:
    """One row per transaction; absent measurements remain empty, never zero."""
    validate_summary(report)
    normalized = list(report["transactions"])
    normalized.sort(key=lambda row: (row["charger_id"], row["id"]))
    records = [_record(COLUMNS)]
    for row in normalized:
        complete = row.get("energy_wh") is not None and row.get("state") in {"completed", "stopped"} and row.get("active") is not True
        cells = [row.get(column) if column != "completeness" else ("complete" if complete else "provisional") for column in COLUMNS]
        records.append(_record([_safe(cell) for cell in cells]))
    return "".join(records)
