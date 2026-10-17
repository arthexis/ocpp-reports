"""CSV compatibility profiles for historical customer reports."""
from __future__ import annotations

import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

HEADERS = ("LOCACION", "CONECTOR", "FECHA INICIO", "FECHA FINAL",
           "WH INICIO", "WH FINAL", "WH USADOS", "% INICIAL",
           "% FINAL", "RAZON FINAL")


def render_audi(report: dict, *, locations: dict | None = None) -> str:
    """Render the Audi ten-column layout; unknown measurements remain blank."""
    from .validation import validate_summary
    validate_summary(report)
    locations = locations or {}
    zone = ZoneInfo(report["period"]["timezone"])
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    for tx in report["transactions"]:
        def when(key: str) -> str:
            value = tx.get(key)
            if not value:
                return ""
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")

        writer.writerow([
            locations.get(tx["charger_id"], ""),
            tx.get("connector_id") if tx.get("connector_id") is not None else "",
            when("started_at"), when("stopped_at"),
            tx.get("meter_start_wh") if tx.get("meter_start_wh") is not None else "",
            tx.get("meter_stop_wh") if tx.get("meter_stop_wh") is not None else "",
            tx.get("energy_wh") if tx.get("energy_wh") is not None else "",
            tx.get("battery_start_pct") if tx.get("battery_start_pct") is not None else "",
            tx.get("battery_stop_pct") if tx.get("battery_stop_pct") is not None else "",
            tx.get("stop_reason") or "",
        ])
    return output.getvalue()
