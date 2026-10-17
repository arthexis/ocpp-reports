"""Client-specific CSV projections; absent evidence stays blank."""
from __future__ import annotations

import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

HEADERS = ("LOCACION", "CONECTOR", "FECHA INICIO", "FECHA FINAL",
           "WH INICIO", "WH FINAL", "WH USADOS", "% INICIAL",
           "% FINAL", "RAZON FINAL")


def render_audi_csv(report: dict) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    zone = ZoneInfo(report["period"]["timezone"])

    def date(value):
        if not value:
            return ""
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(zone).isoformat()

    def cell(value):
        return "" if value is None else value

    for tx in report["transactions"]:
        writer.writerow([
            cell(tx.get("location")), cell(tx.get("connector_id")),
            date(tx.get("started_at")), date(tx.get("stopped_at")),
            cell(tx.get("meter_start_wh")), cell(tx.get("meter_stop_wh")),
            cell(tx.get("energy_wh")), cell(tx.get("soc_start")),
            cell(tx.get("soc_end")), cell(tx.get("stop_reason")),
        ])
    return output.getvalue()
