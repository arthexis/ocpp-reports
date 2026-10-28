"""Client-specific CSV projections; absent evidence stays blank."""
from __future__ import annotations

from zoneinfo import ZoneInfo
from .reports.audi_common import HEADERS, writer_buffer, local_date, blank



def render_audi_csv(report: dict) -> str:
    output, writer = writer_buffer()
    zone = ZoneInfo(report["period"]["timezone"])

    for tx in report["transactions"]:
        writer.writerow([
            blank(tx.get("location")), cell(tx.get("connector_id")),
            local_date(tx.get("started_at"), zone, iso=True), local_date(tx.get("stopped_at"), zone, iso=True),
            cell(tx.get("meter_start_wh")), cell(tx.get("meter_stop_wh")),
            cell(tx.get("energy_wh")), cell(tx.get("soc_start")),
            cell(tx.get("soc_end")), cell(tx.get("stop_reason")),
        ])
    return output.getvalue()
